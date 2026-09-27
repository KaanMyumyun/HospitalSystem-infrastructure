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
BUCKET = "hospitalsystem-tfstate-123456789012"
LOCK = {"ID": "0b1c-lock", "Operation": "OperationTypeApply", "Who": "kaan@laptop",
        "Created": "2026-09-27T10:00:00Z"}

# State lives in a JSON file: resources, whether the state bucket exists, and
# how many times each kind of destroy should still fail. Calls in
# terraform/bootstrap are logged as "terraform bootstrap ...".
# FAKE_DESTROY_LEAVES: a resource the full destroy "succeeds" without deleting.
# FAKE_BUCKET_STAYS: the bootstrap destroy doesn't delete the bucket.
FAKE_TERRAFORM = r'''
import json, os, sys
from pathlib import Path
state_path = os.environ["FAKE_STATE"]
state = json.load(open(state_path))
chdir = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("-chdir=")), os.getcwd())
args = [a for a in sys.argv[1:] if not a.startswith("-chdir=")]
bootstrap = chdir.endswith("bootstrap")
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("terraform " + ("bootstrap " if bootstrap else "") + " ".join(args) + "\n")

def save():
    json.dump(state, open(state_path, "w"))

if args[0] == "init":
    bucket = next((a.split("=", 2)[2] for a in args if a.startswith("-backend-config=bucket=")), None)
    if bucket:  # what terraform init saves for the S3 backend
        record = Path(chdir) / ".terraform/terraform.tfstate"
        record.parent.mkdir(exist_ok=True)
        record.write_text(json.dumps({"backend": {"type": "s3", "config": {"bucket": bucket}}}))
elif bootstrap and args[0] == "apply":
    state["bucket"] = True
    save()
elif bootstrap and args[0] == "destroy":
    if not os.environ.get("FAKE_BUCKET_STAYS"):
        state["bucket"] = False
        save()
elif args[0] in ("plan", "apply", "validate"):
    print(f"fake {args[0]}")
elif args[:2] == ["state", "list"]:
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
    leaves = os.environ.get("FAKE_DESTROY_LEAVES")
    state["resources"] = [r for r in state["resources"] if r != target] if target else [leaves] if leaves else []
    if not target:  # like local_file.ansible_vars
        os.remove(os.environ["FAKE_VARS_FILE"])
    save()
    print("Destroy complete!")
else:
    sys.exit(2)
'''

# FAKE_LOCK: the lock object's content; unset means the state is unlocked.
# FAKE_S3_FAILS: reading the lock fails. FAKE_LIST_BUCKETS_FAILS: so does
# listing the buckets.
FAKE_AWS = r'''
import json, os, re, sys
args = " ".join(sys.argv[1:])
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("aws " + args + "\n")
lock = os.environ.get("FAKE_LOCK")
account = os.environ.get("FAKE_ACCOUNT", "123456789012")
if ("list-objects-v2" in args or args.startswith("s3 cp")) and os.environ.get("FAKE_S3_FAILS") \
        or "list-buckets" in args and os.environ.get("FAKE_LIST_BUCKETS_FAILS"):
    print("An error occurred (AccessDenied)", file=sys.stderr)
    sys.exit(1)
if "s3api list-buckets" in args:
    name = re.search(r"Name=='([^']+)'", args).group(1)
    exists = json.load(open(os.environ["FAKE_STATE"]))["bucket"] and account == "123456789012"
    print(name if exists and name == "hospitalsystem-tfstate-123456789012" else "")
elif "s3api list-objects-v2" in args:
    print("hospitalsystem/terraform.tfstate.tflock" if lock else "None")
elif args.startswith("s3 cp") and lock:
    print(lock)
elif args.startswith("s3 cp"):
    sys.exit(1)
elif "sts get-caller-identity" in args:
    print(account)
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

ENV_LOCAL = """CLOUDFLARE_API_TOKEN=fake-token
HOSPITALSYSTEM_CONNECTION_STRING=Host=db
HOSPITALSYSTEM_JWT_SECRET=fake-key
ALERT_EMAIL=ops@example.com
"""


class FakeRepoTest(TestCase):
    def run_script(self, command, *, resources=RESOURCES, fail=None, stdin="", env=None,
                   bucket=True, backend=BUCKET):
        """Runs COMMAND (script path and arguments) in a fake repo.

        bucket: whether the state bucket exists. backend: the bucket the last
        terraform init in terraform/ used; None before the first init.
        """
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = tmp / "repo"
        (repo / "scripts/lib").mkdir(parents=True)
        for name in ("teardown.sh", "tf.sh", "read-config.py", "lib/config.sh", "lib/state.sh"):
            shutil.copy(ROOT / "scripts" / name, repo / "scripts" / name)
        for name in ("pre-destroy-cleanup.sh", "cleanup-orphans.sh", "account-sweep.py"):
            (repo / "scripts" / name).write_text(FAKE_SCRIPT)
            (repo / "scripts" / name).chmod(0o755)
        (repo / "ansible/group_vars/all").mkdir(parents=True)
        (repo / "ansible/group_vars/all/main.yml").write_text("ingress_name: hospital-ingress\n")
        (repo / "ansible/group_vars/all/terraform.yml").write_text(
            "aws_region: eu-north-1\nmonitoring_alarm_prefix: hospitalsystem\nk8s_namespace: hospitalsystem\n")
        (repo / "terraform/bootstrap").mkdir(parents=True)
        if backend:
            (repo / "terraform/.terraform").mkdir()
            (repo / "terraform/.terraform/terraform.tfstate").write_text(
                json.dumps({"backend": {"type": "s3", "config": {"bucket": backend}}}))
        (repo / ".env.local").write_text(ENV_LOCAL)

        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for tool, body in (("terraform", FAKE_TERRAFORM), ("aws", FAKE_AWS)):
            (bin_dir / tool).write_text(f"#!{sys.executable}\n{body}")
        for tool in ("session-manager-plugin", "docker"):
            (bin_dir / tool).write_text("#!/bin/sh\n")
        for tool in bin_dir.iterdir():
            tool.chmod(0o755)

        state = tmp / "state.json"
        state.write_text(json.dumps({"resources": list(resources), "outputs": OUTPUTS, "fail": fail or {},
                                     "bucket": bucket}))
        self.log = tmp / "calls.log"
        self.log.touch()
        self.repo = repo
        result = subprocess.run(
            ["bash", str(repo / command[0]), *command[1:]], input=stdin, text=True,
            capture_output=True, timeout=60,
            env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp),
                 "FAKE_STATE": str(state), "FAKE_LOG": str(self.log),
                 "FAKE_VARS_FILE": str(repo / "ansible/group_vars/all/terraform.yml"), **(env or {})},
        )
        saved = json.loads(state.read_text())
        self.remaining = saved["resources"]
        self.bucket_exists = saved["bucket"]
        return result

    def calls(self):
        """The logged calls, without the read-only state, lock and bucket reads and AWS login check."""
        skip = ("terraform state list", "terraform output -json", "aws sts get-caller-identity",
                "aws s3api list-objects-v2", "aws s3 cp", "aws s3api list-buckets")
        return [line for line in self.log.read_text().splitlines() if not line.startswith(skip)]

    def assertExit(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)


BUCKET_DELETE = [
    "terraform bootstrap init -input=false",
    f"terraform bootstrap destroy -auto-approve -input=false -var bucket_name={BUCKET}",
]


class TeardownTests(FakeRepoTest):
    def run_teardown(self, *args, stdin="eks-pr1\n", **kwargs):
        return self.run_script(["scripts/teardown.sh", *args], stdin=stdin, **kwargs)

    def test_full_teardown_runs_every_step_in_order(self):
        result = self.run_teardown()
        self.assertExit(result, 0)
        self.assertEqual(self.calls(), [
            "terraform destroy -target=terraform_data.kubernetes_cleanup -auto-approve -input=false -no-color",
            "pre-destroy-cleanup.sh VPC_ID=vpc-1 CLUSTER_NAME= ALARM_PREFIX= --apply",
            "terraform destroy -auto-approve -input=false -no-color",
            # terraform.yml is gone after the destroy; these were read before it.
            "cleanup-orphans.sh VPC_ID= CLUSTER_NAME=eks-pr1 ALARM_PREFIX=hospitalsystem --apply",
            *BUCKET_DELETE,
            "account-sweep.py VPC_ID= CLUSTER_NAME= ALARM_PREFIX= ",
        ])
        self.assertEqual(self.remaining, [])
        self.assertFalse(self.bucket_exists)
        self.assertIn(f"Deleted {BUCKET}.", result.stdout)
        self.assertIn("Clean: the stack is gone", result.stdout)
        logs = list((self.repo / ".generated/teardown").glob("*/*.log"))
        self.assertEqual(len(logs), 6)

    def test_wrong_or_missing_confirmation_changes_nothing(self):
        for stdin in ("eks-pr2\n", ""):
            with self.subTest(stdin=stdin):
                result = self.run_teardown(stdin=stdin)
                self.assertExit(result, 1)
                self.assertIn("Not confirmed. Nothing was changed.", result.stderr)
                self.assertEqual(self.calls(), [])
                self.assertEqual(self.remaining, RESOURCES)
                self.assertTrue(self.bucket_exists)

    def test_yes_skips_the_question(self):
        result = self.run_teardown("--yes", stdin="")
        self.assertExit(result, 0)
        self.assertEqual(self.remaining, [])
        self.assertFalse(self.bucket_exists)

    def test_empty_state_deletes_the_bucket_and_sweeps(self):
        result = self.run_teardown(resources=[], stdin="")
        self.assertExit(result, 0)
        self.assertEqual([call.split(" VPC_ID")[0] for call in self.calls()],
                         ["cleanup-orphans.sh", *BUCKET_DELETE, "account-sweep.py"])
        self.assertFalse(self.bucket_exists)

    def test_no_state_bucket_only_cleans_orphans_and_sweeps(self):
        result = self.run_teardown(resources=[], bucket=False, stdin="")
        self.assertExit(result, 0)
        self.assertIn(f"No state bucket ({BUCKET}), so nothing is in Terraform state.", result.stdout)
        self.assertEqual([call.split()[0] for call in self.calls()], ["cleanup-orphans.sh", "account-sweep.py"])
        self.assertNotIn("terraform", self.log.read_text())

    def test_unlistable_buckets_stop_first(self):
        result = self.run_teardown(env={"FAKE_LIST_BUCKETS_FAILS": "1"})
        self.assertExit(result, 1)
        self.assertIn("Could not list the S3 buckets", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_uninitialized_backend_is_initialized_first(self):
        for backend, extra in ((None, ""), ("hospitalsystem-tfstate-999999999999", " -reconfigure")):
            with self.subTest(backend=backend):
                result = self.run_teardown(backend=backend)
                self.assertExit(result, 0)
                self.assertEqual(self.calls()[0], f"terraform init -input=false -backend-config=bucket={BUCKET}{extra}")
                self.assertEqual(self.remaining, [])

    def test_other_account_reads_its_own_state_bucket(self):
        result = self.run_teardown(env={"FAKE_ACCOUNT": "999999999999"})
        self.assertExit(result, 0)
        self.assertIn("No state bucket (hospitalsystem-tfstate-999999999999)", result.stdout)
        self.assertFalse(any(call.startswith("terraform") for call in self.calls()))
        self.assertEqual(self.remaining, RESOURCES)

    def test_state_lock_stops_first_and_says_how_to_unlock(self):
        result = self.run_teardown(env={"FAKE_LOCK": json.dumps(LOCK)})
        self.assertExit(result, 1)
        self.assertIn("locked by kaan@laptop (apply since 2026-09-27T10:00:00Z)", result.stderr)
        self.assertIn("./scripts/tf.sh force-unlock 0b1c-lock", result.stderr)
        self.assertIn(f"aws s3api list-objects-v2 --bucket {BUCKET} "
                      "--prefix hospitalsystem/terraform.tfstate.tflock", self.log.read_text())
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.remaining, RESOURCES)
        self.assertTrue(self.bucket_exists)

    def test_unreadable_lock_stops_first(self):
        result = self.run_teardown(env={"FAKE_S3_FAILS": "1"})
        self.assertExit(result, 1)
        self.assertIn(f"Could not check the state lock in s3://{BUCKET}", result.stderr)
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
        self.assertTrue(self.bucket_exists)

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
        self.assertFalse(any(call.startswith(("account-sweep.py", "terraform bootstrap")) for call in self.calls()))
        self.assertTrue(self.bucket_exists)

    def test_state_left_after_the_destroy_keeps_the_bucket(self):
        result = self.run_teardown(env={"FAKE_DESTROY_LEAVES": "aws_vpc.kubes"})
        self.assertExit(result, 1)
        self.assertIn("Terraform state still has 1 resource(s).", result.stdout)
        self.assertIn(f"Kept {BUCKET}", result.stdout)
        self.assertIn("Not clean: 1 problem(s)", result.stdout)
        self.assertFalse(any(call.startswith("terraform bootstrap") for call in self.calls()))
        self.assertTrue(self.bucket_exists)

    def test_bucket_left_after_its_destroy_is_not_clean(self):
        result = self.run_teardown(env={"FAKE_BUCKET_STAYS": "1"})
        self.assertExit(result, 1)
        self.assertIn(f"The state bucket {BUCKET} is still there", result.stdout)
        self.assertIn("Not clean: 1 problem(s)", result.stdout)

    def test_billable_sweep_or_failed_orphan_cleanup_is_not_clean(self):
        for script in ("ACCOUNT_SWEEP_PY", "CLEANUP_ORPHANS_SH"):
            with self.subTest(script=script):
                result = self.run_teardown(env={f"FAKE_{script}_FAILS": "1"})
                self.assertExit(result, 1)
                self.assertIn("Not clean: 1 problem(s)", result.stdout)
                self.assertEqual(self.remaining, [])


class TfWrapperTests(FakeRepoTest):
    def run_tf(self, *args, **kwargs):
        return self.run_script(["scripts/tf.sh", *args], **kwargs)

    def test_plan_or_apply_without_a_bucket_creates_it_and_initializes_first(self):
        for command in ("plan", "apply"):
            with self.subTest(command=command):
                result = self.run_tf(command, "-no-color", bucket=False, backend=None)
                self.assertExit(result, 0)
                self.assertEqual(self.calls(), [
                    "terraform bootstrap init -input=false",
                    f"terraform bootstrap apply -auto-approve -input=false -var bucket_name={BUCKET}",
                    f"terraform init -input=false -backend-config=bucket={BUCKET}",
                    f"terraform {command} -no-color",
                ])
                self.assertTrue(self.bucket_exists)
                self.assertEqual(result.stdout, f"fake {command}\n")

    def test_existing_bucket_and_backend_run_terraform_alone(self):
        result = self.run_tf("plan")
        self.assertExit(result, 0)
        self.assertEqual(self.calls(), ["terraform plan"])

    def test_init_passes_the_bucket(self):
        result = self.run_tf("init", "-migrate-state", backend=None)
        self.assertExit(result, 0)
        self.assertEqual(self.calls(), [f"terraform init -migrate-state -backend-config=bucket={BUCKET}"])

    def test_reading_or_destroying_without_a_bucket_stops(self):
        for command in (["output", "-json"], ["state", "list"], ["destroy"]):
            with self.subTest(command=command):
                result = self.run_tf(*command, bucket=False)
                self.assertExit(result, 1)
                self.assertIn(f"There is no state bucket ({BUCKET})", result.stderr)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.bucket_exists)

    def test_offline_commands_skip_aws(self):
        result = self.run_tf("validate", bucket=False, backend=None)
        self.assertExit(result, 0)
        self.assertEqual(self.log.read_text(), "terraform validate\n")


if __name__ == "__main__":
    main()
