"""Run scripts/teardown.sh and the real tf.sh in a copy of the repo with fakes.

terraform, aws, and the cleanup, orphan and sweep scripts are fakes that log
every call, so the tests check the order of the steps and where a failure
stops the teardown. Nothing reaches AWS or a real Terraform state.
"""

from pathlib import Path
from unittest import TestCase, main
import json
import os
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ["aws_eks_cluster.main", "aws_vpc.kubes", "terraform_data.kubernetes_cleanup"]
OUTPUTS = {"account_id": "123456789012", "cluster_name": "eks-pr1", "vpc_id": "vpc-1",
           "ops_instance_id": "i-ops"}

# State lives in a JSON file: resources, plus how many times each kind of
# destroy should still fail.
FAKE_TERRAFORM = r'''
import json, os, sys
state_path = os.environ["FAKE_STATE"]
state = json.load(open(state_path))
args = [a for a in sys.argv[1:] if not a.startswith("-chdir=")]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("terraform " + " ".join(args) + "\n")

def save():
    json.dump(state, open(state_path, "w"))

if args[:2] == ["state", "list"]:
    print("\n".join(state["resources"]))
elif args[:2] == ["output", "-json"]:
    print(json.dumps({k: {"value": v} for k, v in state["outputs"].items()} if state["resources"] else {}))
elif args[0] == "destroy":
    target = next((a.split("=", 1)[1] for a in args if a.startswith("-target=")), None)
    kind = "target" if target else "full"
    if state["fail"].get(kind, 0) > 0:
        state["fail"][kind] -= 1
        save()
        print(f"Error: fake {kind} destroy failure")
        sys.exit(1)
    state["resources"] = [r for r in state["resources"] if r != target] if target else []
    if not target:  # like local_file.ansible_vars
        os.remove(os.environ["FAKE_VARS_FILE"])
    save()
    print("Destroy complete!")
else:
    sys.exit(2)
'''

FAKE_AWS = r'''
import os, sys
args = " ".join(sys.argv[1:])
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("aws " + args + "\n")
if "sts get-caller-identity" in args:
    print(os.environ.get("FAKE_ACCOUNT", "123456789012"))
elif "describe-security-groups" in args:
    print("sg-cluster")
'''

# FAKE_<NAME>_FAILS: how many runs of that script fail before one succeeds.
FAKE_SCRIPT = r'''#!/usr/bin/env bash
name="$(basename "$0")"
counter="$FAKE_LOG.$name"
printf '%s VPC_ID=%s CLUSTER_NAME=%s ALARM_PREFIX=%s %s\n' "$name" "${VPC_ID:-}" "${CLUSTER_NAME:-}" \
  "${ALARM_PREFIX:-}" "$*" >> "$FAKE_LOG"
var="FAKE_$(tr 'a-z.-' 'A-Z__' <<<"$name")_FAILS"
seen="$(cat "$counter" 2>/dev/null || echo 0)"
echo $((seen + 1)) > "$counter"
[ "$seen" -ge "${!var:-0}" ]
'''


class TeardownTests(TestCase):
    def run_teardown(self, *args, resources=RESOURCES, fail=None, stdin="eks-pr1\n", env=None, lock=False):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = tmp / "repo"
        (repo / "scripts/lib").mkdir(parents=True)
        for name in ("teardown.sh", "tf.sh", "read-config.py", "lib/config.sh"):
            shutil.copy(ROOT / "scripts" / name, repo / "scripts" / name)
        for name in ("pre-destroy-cleanup.sh", "cleanup-orphans.sh", "account-sweep.py"):
            (repo / "scripts" / name).write_text(FAKE_SCRIPT)
            (repo / "scripts" / name).chmod(0o755)
        (repo / "ansible/group_vars/all").mkdir(parents=True)
        (repo / "ansible/group_vars/all/main.yml").write_text("ingress_name: hospital-ingress\n")
        (repo / "ansible/group_vars/all/terraform.yml").write_text(
            "aws_region: eu-north-1\nmonitoring_alarm_prefix: hospitalsystem\nk8s_namespace: hospitalsystem\n")
        (repo / "terraform").mkdir()
        if lock:
            (repo / "terraform/.terraform.tfstate.lock.info").write_text("{}")
        (repo / ".env.local").write_text("CLOUDFLARE_API_TOKEN=fake-token\n")

        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for tool, body in (("terraform", FAKE_TERRAFORM), ("aws", FAKE_AWS)):
            (bin_dir / tool).write_text(f"#!{sys.executable}\n{body}")
        (bin_dir / "session-manager-plugin").write_text("#!/bin/sh\n")
        for tool in bin_dir.iterdir():
            tool.chmod(0o755)

        state = tmp / "state.json"
        state.write_text(json.dumps({"resources": list(resources), "outputs": OUTPUTS, "fail": fail or {}}))
        self.log = tmp / "calls.log"
        self.log.touch()
        self.repo = repo
        result = subprocess.run(
            ["bash", str(repo / "scripts/teardown.sh"), *args], input=stdin, text=True,
            capture_output=True, timeout=60,
            env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp),
                 "FAKE_STATE": str(state), "FAKE_LOG": str(self.log),
                 "FAKE_VARS_FILE": str(repo / "ansible/group_vars/all/terraform.yml"), **(env or {})},
        )
        self.remaining = json.loads(state.read_text())["resources"]
        return result

    def calls(self):
        """The logged calls, without the read-only state reads and AWS login check."""
        skip = ("terraform state list", "terraform output -json", "aws sts get-caller-identity")
        return [line for line in self.log.read_text().splitlines() if not line.startswith(skip)]

    def assertExit(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def test_full_teardown_runs_every_step_in_order(self):
        result = self.run_teardown()
        self.assertExit(result, 0)
        self.assertEqual(self.calls(), [
            "terraform destroy -target=terraform_data.kubernetes_cleanup -auto-approve -input=false -no-color",
            "pre-destroy-cleanup.sh VPC_ID=vpc-1 CLUSTER_NAME= ALARM_PREFIX= --apply",
            "terraform destroy -auto-approve -input=false -no-color",
            # terraform.yml is gone after the destroy; these were read before it.
            "cleanup-orphans.sh VPC_ID= CLUSTER_NAME=eks-pr1 ALARM_PREFIX=hospitalsystem --apply",
            "account-sweep.py VPC_ID= CLUSTER_NAME= ALARM_PREFIX= ",
        ])
        self.assertEqual(self.remaining, [])
        self.assertIn("Clean: the stack is gone", result.stdout)
        logs = list((self.repo / ".generated/teardown").glob("*/*.log"))
        self.assertEqual(len(logs), 5)

    def test_wrong_or_missing_confirmation_changes_nothing(self):
        for stdin in ("eks-pr2\n", ""):
            with self.subTest(stdin=stdin):
                result = self.run_teardown(stdin=stdin)
                self.assertExit(result, 1)
                self.assertIn("Not confirmed. Nothing was changed.", result.stderr)
                self.assertEqual(self.calls(), [])
                self.assertEqual(self.remaining, RESOURCES)

    def test_yes_skips_the_question(self):
        result = self.run_teardown("--yes", stdin="")
        self.assertExit(result, 0)
        self.assertEqual(self.remaining, [])

    def test_empty_state_only_cleans_orphans_and_sweeps(self):
        result = self.run_teardown(resources=[], stdin="")
        self.assertExit(result, 0)
        self.assertEqual([call.split()[0] for call in self.calls()], ["cleanup-orphans.sh", "account-sweep.py"])

    def test_lock_left_by_an_interrupted_run_stops_first(self):
        result = self.run_teardown(lock=True)
        self.assertExit(result, 1)
        self.assertIn("was interrupted", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_other_account_stops_first(self):
        result = self.run_teardown(env={"FAKE_ACCOUNT": "999999999999"})
        self.assertExit(result, 1)
        self.assertIn("logged in to account 999999999999, but the stack is in 123456789012", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_old_count_address_of_the_cleanup_is_targeted(self):
        result = self.run_teardown(resources=["aws_vpc.kubes", "terraform_data.kubernetes_cleanup[0]"])
        self.assertExit(result, 0)
        self.assertIn("terraform destroy -target=terraform_data.kubernetes_cleanup[0] -auto-approve -input=false -no-color",
                      self.calls())

    def test_failed_kubernetes_cleanup_stops_before_the_destroy(self):
        result = self.run_teardown(fail={"target": 1})
        self.assertExit(result, 1)
        self.assertIn("The Kubernetes cleanup failed, and the cluster is still up", result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.remaining, RESOURCES)

    def test_pre_destroy_cleanup_is_retried_once(self):
        result = self.run_teardown(env={"FAKE_PRE_DESTROY_CLEANUP_SH_FAILS": "1"})
        self.assertExit(result, 0)
        self.assertEqual(sum(call.startswith("pre-destroy-cleanup.sh") for call in self.calls()), 2)
        self.assertEqual(self.remaining, [])

    def test_pre_destroy_cleanup_failing_twice_stops_before_the_destroy(self):
        result = self.run_teardown(env={"FAKE_PRE_DESTROY_CLEANUP_SH_FAILS": "2"})
        self.assertExit(result, 1)
        self.assertIn("couldn't be deleted", result.stderr)
        self.assertNotIn("terraform destroy -auto-approve -input=false -no-color", self.calls())
        self.assertEqual(self.remaining, ["aws_eks_cluster.main", "aws_vpc.kubes"])

    def test_failed_destroy_cleans_up_deletes_the_cluster_group_and_retries(self):
        result = self.run_teardown(fail={"full": 1})
        self.assertExit(result, 0)
        calls = self.calls()
        destroy = "terraform destroy -auto-approve -input=false -no-color"
        first, second = [i for i, call in enumerate(calls) if call == destroy]
        between = calls[first + 1:second]
        self.assertTrue(between[0].startswith("pre-destroy-cleanup.sh VPC_ID=vpc-1"))
        self.assertIn("Values=eks-cluster-sg-eks-pr1-*", between[1])
        self.assertIn("aws ec2 delete-security-group --region eu-north-1 --group-id sg-cluster", between)
        self.assertEqual(self.remaining, [])

    def test_destroy_failing_twice_stops_and_shows_what_is_left(self):
        result = self.run_teardown(fail={"full": 2})
        self.assertExit(result, 1)
        self.assertIn("The destroy failed twice", result.stderr)
        self.assertIn("Still in Terraform state:\n  aws_eks_cluster.main", result.stdout)
        self.assertFalse(any(call.startswith("account-sweep.py") for call in self.calls()))

    def test_billable_sweep_or_failed_orphan_cleanup_is_not_clean(self):
        for script in ("ACCOUNT_SWEEP_PY", "CLEANUP_ORPHANS_SH"):
            with self.subTest(script=script):
                result = self.run_teardown(env={f"FAKE_{script}_FAILS": "1"})
                self.assertExit(result, 1)
                self.assertIn("Not clean: 1 problem(s)", result.stdout)
                self.assertEqual(self.remaining, [])


if __name__ == "__main__":
    main()
