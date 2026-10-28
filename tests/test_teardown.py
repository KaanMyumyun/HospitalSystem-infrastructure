"""Run scripts/teardown.sh and the real tf.sh in a copy of the repo with fakes.

terraform, aws, ansible-playbook, and the cleanup, orphan and sweep scripts are
fakes that log every call, so the tests check the order of the steps and where
a failure stops the teardown. Nothing reaches AWS or a real Terraform state.
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
RESOURCES = ["module.eks.aws_eks_cluster.main", "module.network.aws_vpc.main", "terraform_data.kubernetes_cleanup",
             "aws_db_instance.main"]
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
if os.environ.get("FAKE_ENV_LOG"):  # what tf.sh passed on
    with open(os.environ["FAKE_ENV_LOG"], "a") as env_log:
        env_log.write(json.dumps({"db_password": os.environ.get("TF_VAR_db_password")}) + "\n")

def save():
    json.dump(state, open(state_path, "w"))

if args[0] == "init":
    bucket = next((a.split("=", 2)[2] for a in args if a.startswith("-backend-config=bucket=")), None)
    if bucket:  # what terraform init saves for the S3 backend
        record = Path(chdir) / ".terraform/terraform.tfstate"
        record.parent.mkdir(exist_ok=True)
        record.write_text(json.dumps({"backend": {"type": "s3", "config": {"bucket": bucket}}}))
elif bootstrap and args[0] == "apply":
    # Like terraform: without -auto-approve, only "yes" on stdin applies.
    if "-auto-approve" not in args and sys.stdin.readline().strip() != "yes":
        print("Error: Apply cancelled.")
        sys.exit(1)
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

# Logs the playbook, the directory it ran in (ansible.cfg is read from there)
# and the database password it got. FAKE_ANSIBLE_PLAYBOOK_FAILS: how many runs
# fail before one succeeds.
FAKE_ANSIBLE_PLAYBOOK = r'''#!/usr/bin/env bash
printf 'ansible-playbook %s (in %s, password=%s)\n' "$*" "$(basename "$PWD")" \
  "${HOSPITALSYSTEM_DB_PASSWORD:-}" >> "$FAKE_LOG"
counter="$FAKE_LOG.ansible-playbook"
seen="$(cat "$counter" 2>/dev/null || echo 0)"
echo $((seen + 1)) > "$counter"
[ "$seen" -ge "${FAKE_ANSIBLE_PLAYBOOK_FAILS:-0}" ]
'''

ENV_LOCAL = """CLOUDFLARE_API_TOKEN=fake-token
HOSPITALSYSTEM_DB_PASSWORD=fake-password
HOSPITALSYSTEM_JWT_SECRET=fake-key
ALERT_EMAIL=ops@example.com
"""


class FakeRepoTest(TestCase):
    def run_script(self, command, *, resources=RESOURCES, fail=None, stdin="", env=None,
                   bucket=True, backend=BUCKET, env_local=ENV_LOCAL):
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
        (repo / ".env.local").write_text(env_local)

        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for tool, body in (("terraform", FAKE_TERRAFORM), ("aws", FAKE_AWS)):
            (bin_dir / tool).write_text(f"#!{sys.executable}\n{body}")
        (bin_dir / "ansible-playbook").write_text(FAKE_ANSIBLE_PLAYBOOK)
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
# Run from the repository root, with the password from .env.local.
SAVE = "ansible-playbook ansible/playbooks/dump-database.yml (in repo, password=fake-password)"
CLEANUP = "terraform destroy -target=terraform_data.kubernetes_cleanup -auto-approve -input=false -no-color"


class TeardownTests(FakeRepoTest):
    # stdin is empty, so a question would get no answer and stop the teardown.
    def run_teardown(self, *args, **kwargs):
        return self.run_script(["scripts/teardown.sh", *args], **kwargs)

    def test_full_teardown_runs_every_step_in_order(self):
        result = self.run_teardown()
        self.assertExit(result, 0)
        self.assertEqual(self.calls(), [
            SAVE,
            CLEANUP,
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
        self.assertEqual(len(logs), 7)

    def test_yes_from_older_commands_is_still_accepted(self):
        result = self.run_teardown("--yes")
        self.assertExit(result, 0)
        self.assertEqual(self.calls()[:2], [SAVE, CLEANUP])
        self.assertEqual(self.remaining, [])
        self.assertFalse(self.bucket_exists)

    def test_empty_state_deletes_the_bucket_and_sweeps(self):
        result = self.run_teardown(resources=[])
        self.assertExit(result, 0)
        self.assertEqual([call.split(" VPC_ID")[0] for call in self.calls()],
                         ["cleanup-orphans.sh", *BUCKET_DELETE, "account-sweep.py"])
        self.assertFalse(self.bucket_exists)

    def test_no_state_bucket_only_cleans_orphans_and_sweeps(self):
        result = self.run_teardown(resources=[], bucket=False)
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

    def test_failed_save_stops_before_anything_is_destroyed(self):
        result = self.run_teardown(env={"FAKE_ANSIBLE_PLAYBOOK_FAILS": "1"})
        self.assertExit(result, 1)
        self.assertIn("The database couldn't be saved, and nothing was destroyed", result.stderr)
        self.assertIn("--skip-dump", result.stderr)
        self.assertEqual(self.calls(), [SAVE])
        self.assertEqual(self.remaining, RESOURCES)
        self.assertTrue(self.bucket_exists)

    def test_skip_dump_destroys_the_database_without_saving_it(self):
        result = self.run_teardown("--skip-dump")
        self.assertExit(result, 0)
        self.assertIn("Skipped (--skip-dump): seed.sql stays as it is.", result.stdout)
        self.assertEqual(self.calls()[0], CLEANUP)
        self.assertEqual(self.remaining, [])

    def test_save_is_skipped_without_a_database_or_a_cluster(self):
        for resources, message in (
            (["module.eks.aws_eks_cluster.main", "module.network.aws_vpc.main"], "No database in state"),
            (["aws_db_instance.main", "module.network.aws_vpc.main"], "The cluster is gone, so nothing can reach the database"),
        ):
            with self.subTest(resources=resources):
                result = self.run_teardown(resources=resources)
                self.assertExit(result, 0)
                self.assertIn(f"{message}; seed.sql stays as it is.", result.stdout)
                self.assertNotIn(SAVE, self.calls())
                self.assertEqual(self.remaining, [])

    def test_database_is_saved_with_the_cluster_at_its_old_address(self):
        # A stack built before the cluster moved into terraform/modules/eks.
        result = self.run_teardown(resources=["aws_eks_cluster.main", "aws_vpc.kubes", "aws_db_instance.main"])
        self.assertExit(result, 0)
        self.assertEqual(self.calls()[0], SAVE)

    def test_old_count_address_of_the_cleanup_is_targeted(self):
        result = self.run_teardown(resources=["aws_vpc.kubes", "terraform_data.kubernetes_cleanup[0]"])
        self.assertExit(result, 0)
        self.assertIn("terraform destroy -target=terraform_data.kubernetes_cleanup[0] -auto-approve -input=false -no-color",
                      self.calls())

    def test_failed_kubernetes_cleanup_stops_before_the_destroy(self):
        result = self.run_teardown(fail={"target": 1})
        self.assertExit(result, 1)
        self.assertIn("The Kubernetes cleanup failed, and the cluster is still up", result.stderr)
        self.assertEqual(self.calls(), [SAVE, CLEANUP])
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
        self.assertEqual(self.remaining, ["module.eks.aws_eks_cluster.main", "module.network.aws_vpc.main",
                                          "aws_db_instance.main"])

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
        self.assertIn("Still in Terraform state:\n  module.eks.aws_eks_cluster.main", result.stdout)
        self.assertFalse(any(call.startswith(("account-sweep.py", "terraform bootstrap")) for call in self.calls()))
        self.assertTrue(self.bucket_exists)

    def test_state_left_after_the_destroy_keeps_the_bucket(self):
        result = self.run_teardown(env={"FAKE_DESTROY_LEAVES": "module.network.aws_vpc.main"})
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
                result = self.run_tf(command, "-no-color", bucket=False, backend=None, stdin="yes\n")
                self.assertExit(result, 0)
                self.assertEqual(self.calls(), [
                    "terraform bootstrap init -input=false",
                    f"terraform bootstrap apply -var bucket_name={BUCKET}",
                    f"terraform init -input=false -backend-config=bucket={BUCKET}",
                    f"terraform {command} -no-color",
                ])
                self.assertTrue(self.bucket_exists)
                self.assertEqual(result.stdout, f"fake {command}\n")

    def test_declining_the_bucket_stops_before_the_command(self):
        for command in ("plan", "apply"):
            with self.subTest(command=command):
                result = self.run_tf(command, bucket=False, backend=None, stdin="no\n")
                self.assertExit(result, 1)
                self.assertIn(f"The state bucket wasn't created, so terraform {command} didn't run.",
                              result.stderr)
                self.assertEqual(self.calls(), [
                    "terraform bootstrap init -input=false",
                    f"terraform bootstrap apply -var bucket_name={BUCKET}",
                ])
                self.assertFalse(self.bucket_exists)

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

    def test_the_database_password_reaches_terraform(self):
        env_log = Path(self.enterContext(tempfile.TemporaryDirectory())) / "env.log"
        result = self.run_tf("plan", env={"FAKE_ENV_LOG": str(env_log)})
        self.assertExit(result, 0)
        self.assertEqual(json.loads(env_log.read_text()), {"db_password": "fake-password"})

    def test_plan_and_apply_need_the_database_password_but_destroy_does_not(self):
        env_local = ENV_LOCAL.replace("HOSPITALSYSTEM_DB_PASSWORD=fake-password\n", "")
        for command in ("plan", "apply"):
            with self.subTest(command=command):
                result = self.run_tf(command, env_local=env_local)
                self.assertExit(result, 1)
                self.assertIn("HOSPITALSYSTEM_DB_PASSWORD", result.stderr)
                self.assertEqual(self.calls(), [])
        result = self.run_tf("destroy", env_local=env_local)
        self.assertExit(result, 0)
        self.assertEqual(self.calls(), ["terraform destroy"])


if __name__ == "__main__":
    main()
