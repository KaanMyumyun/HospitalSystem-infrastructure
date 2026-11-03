#!/usr/bin/env python3
"""Save the database, destroy the stack and its state bucket, then sweep AWS.

Usage: scripts/teardown.py [--skip-dump]

The teardown starts right away. --skip-dump destroys the database without
saving it into seed.sql. --yes is accepted for older commands but does nothing.
Each step's output is also saved under .generated/teardown/.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from lib.commands import DROP, MERGE, SHOW, ScriptError, entrypoint, run
from lib.config import REPO_ROOT, setting
from lib.env import load_env
from lib.state import ensure_backend, state_bucket, state_bucket_exists

SCRIPTS = REPO_ROOT / "scripts"
TF_DIR = REPO_ROOT / "terraform"
LOCK_KEY = "hospitalsystem/terraform.tfstate.tflock"


class TeardownStopped(Exception):
    pass


def stop(message):
    raise TeardownStopped(message)


class Teardown:
    def __init__(self, skip_dump):
        self.skip_dump = skip_dump
        # Read these before Terraform deletes terraform.yml.
        self.region = setting("AWS_REGION", "aws_region", "eu-north-1")
        self.alarm_prefix = setting("ALARM_PREFIX", "monitoring_alarm_prefix")
        self.namespace = setting("K8S_NAMESPACE", "k8s_namespace")
        self.ingress = setting("INGRESS_NAME", "ingress_name")
        self.log_dir = REPO_ROOT / ".generated/teardown" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.bucket = ""
        self.have_bucket = False
        self.cluster = ""
        self.vpc = ""
        self.ops_instance = ""

    @staticmethod
    def step(title):
        print(f"\n== {title} ==")

    def run_logged(self, name, command, *, env=None, cwd=None):
        """Stream a command to the terminal and its step log; return success."""
        log = self.log_dir / f"{name}.log"
        print(f"(log: {log.relative_to(REPO_ROOT)})")
        with log.open("w", encoding="utf-8") as saved:
            try:
                process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, errors="replace")
            except OSError as error:
                print(error)
                saved.write(f"{error}\n")
                return False
            for line in process.stdout:
                print(line, end="")
                saved.write(line)
            return process.wait() == 0

    def state_list(self, *, stderr=SHOW):
        return run(["terraform", f"-chdir={TF_DIR}", "state", "list"], stderr=stderr)

    def state_lock(self):
        found = run(["aws", "s3api", "list-objects-v2", "--bucket", self.bucket, "--prefix", LOCK_KEY,
                     "--query", f"Contents[?Key=='{LOCK_KEY}'].Key", "--output", "text"], stderr=SHOW)
        if not found.ok:
            return False, ""
        if not found.out or found.out == "None":
            return True, ""
        lock = run(["aws", "s3", "cp", f"s3://{self.bucket}/{LOCK_KEY}", "-"], stderr=SHOW)
        return lock.ok, lock.out

    def save_database(self):
        env = os.environ.copy()
        env_file = REPO_ROOT / ".env.local"
        if env_file.is_file():
            try:
                env = load_env(env_file)
            except ScriptError as error:
                print(error)
                return False
        return self.run_logged("2-dump-database", ["ansible-playbook", "ansible/playbooks/dump-database.yml"],
                               cwd=REPO_ROOT, env=env)

    def pre_destroy_cleanup(self, name):
        if not self.vpc:
            print("No VPC in state, so nothing in it to clean up.")
            return True
        env = {**os.environ, "VPC_ID": self.vpc}
        command = [str(SCRIPTS / "pre-destroy-cleanup.py"), "--apply"]
        if self.run_logged(name, command, env=env):
            return True
        print("\nRetrying once.")
        return self.run_logged(f"{name}-retry", command, env=env)

    def delete_cluster_security_group(self):
        if not self.vpc or not self.cluster:
            return True
        groups = run(["aws", "ec2", "describe-security-groups", "--region", self.region, "--filters",
                      f"Name=vpc-id,Values={self.vpc}", f"Name=group-name,Values=eks-cluster-sg-{self.cluster}-*",
                      "--query", "SecurityGroups[].GroupId", "--output", "text"], stderr=SHOW)
        if not groups.ok:
            return False
        for group in groups.out.split():
            print(f"Deleting EKS cluster security group {group}")
            deleted = run(["aws", "ec2", "delete-security-group", "--region", self.region, "--group-id", group],
                          stderr=SHOW)
            if deleted.out:
                print(deleted.out)
            if not deleted.ok:
                return False
        return True

    def show_what_is_left(self):
        print("\nStill in Terraform state:")
        state = self.state_list()
        for line in state.out.splitlines():
            print(f"  {line}")
        if self.vpc:
            print(f"\nNetwork interfaces still in {self.vpc}:")
            interfaces = run(["aws", "ec2", "describe-network-interfaces", "--region", self.region,
                              "--filters", f"Name=vpc-id,Values={self.vpc}", "--query",
                              "NetworkInterfaces[].[NetworkInterfaceId, Status, Description]", "--output", "text"],
                             stderr=SHOW)
            for line in interfaces.out.splitlines():
                print(f"  {line}")

    def check(self):
        self.step("1/8 Checks")
        identity = run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"])
        if not identity.ok:
            stop(f"No usable AWS credentials ({identity.out}). Run: aws login")
        self.bucket = state_bucket(identity.out)
        bucket_status = state_bucket_exists(self.bucket)
        if bucket_status == 2:
            stop("Could not list the S3 buckets (see the error above).")
        self.have_bucket = bucket_status == 0
        resources = ""
        outputs = {}
        if not self.have_bucket:
            print(f"No state bucket ({self.bucket}), so nothing is in Terraform state.")
        else:
            initialized = ensure_backend(self.bucket)
            if not initialized.ok:
                stop(f"terraform init failed: {initialized.out}")
            locked, lock = self.state_lock()
            if not locked:
                stop(f"Could not check the state lock in s3://{self.bucket} (see the error above).")
            if lock:
                try:
                    data = json.loads(lock)
                except ValueError:
                    stop("Could not read the state lock details; wait for the active Terraform run to finish.")
                who = data.get("Who", "?")
                operation = data.get("Operation", "").removeprefix("OperationType").lower() or "?"
                created = data.get("Created", "?")
                lock_id = data.get("ID", "?")
                stop(f"The state is locked by {who} ({operation} since {created}). If that run is still going, "
                     "wait for it to finish. If it was interrupted, unlock it "
                     f"(./scripts/tf.py force-unlock {lock_id}) and run this again.")
            listed = self.state_list(stderr=MERGE)
            if not listed.ok:
                stop(f"Could not read the Terraform state: {listed.out}")
            resources = listed.out
            out = run(["terraform", f"-chdir={TF_DIR}", "output", "-json"])
            if not out.ok:
                stop(f"Could not read the Terraform outputs: {out.out}")
            try:
                outputs = json.loads(out.out)
            except ValueError:
                stop("Could not parse the Terraform outputs")
        def output(name):
            return (outputs.get(name) or {}).get("value", "")
        self.cluster = output("cluster_name")
        self.vpc = output("vpc_id")
        self.ops_instance = output("ops_instance_id")
        count = sum(bool(line) for line in resources.splitlines())
        print(f"Account:   {identity.out}\nRegion:    {self.region}\nCluster:   {self.cluster or 'none'}"
              f"\nVPC:       {self.vpc or 'none'}\nIn state:  {count} resource(s)")
        return resources.splitlines()

    def run(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        resources = self.check()
        if resources:
            self.step("2/8 Save the database into seed.sql")
            if self.skip_dump:
                print("Skipped (--skip-dump): seed.sql stays as it is.")
            elif "aws_db_instance.main" not in resources:
                print("No database in state; seed.sql stays as it is.")
            elif not any(name in resources for name in ("module.eks.aws_eks_cluster.main", "aws_eks_cluster.main")):
                print("The cluster is gone, so nothing can reach the database; seed.sql stays as it is.")
            elif not self.save_database():
                stop("The database couldn't be saved, and nothing was destroyed. Fix the cause shown above and run "
                     "this again, or run with --skip-dump to destroy the database without saving it.")

            self.step("3/8 Kubernetes cleanup while the cluster is up")
            cleanup = next((name for name in resources if re.fullmatch(r"terraform_data\.kubernetes_cleanup(\[0\])?", name)), "")
            if not cleanup:
                print("terraform_data.kubernetes_cleanup is not in state; skipping.")
            elif not self.run_logged("3-kubernetes-cleanup", [str(SCRIPTS / "tf.py"), "destroy",
                                                         f"-target={cleanup}", "-auto-approve", "-input=false",
                                                         "-no-color"]):
                stop("The Kubernetes cleanup failed, and the cluster is still up. Fix the cause shown above and "
                     "run this again.")

            self.step("4/8 Pre-destroy cleanup")
            if not self.pre_destroy_cleanup("4-pre-destroy-cleanup"):
                stop("Something blocking the destroy couldn't be deleted (see above). Fix it and run this again.")

            self.step("5/8 terraform destroy")
            command = [str(SCRIPTS / "tf.py"), "destroy", "-auto-approve", "-input=false", "-no-color"]
            if not self.run_logged("5-destroy", command):
                print("\nThe destroy failed. Cleaning up what usually blocks it, then retrying once.")
                self.pre_destroy_cleanup("5-pre-destroy-cleanup-again")
                if not self.delete_cluster_security_group():
                    print("Could not delete the EKS cluster security group.")
                if not self.run_logged("5-destroy-retry", command):
                    self.show_what_is_left()
                    stop("The destroy failed twice. The log above says what blocked it.")

        self.step("6/8 Leftovers")
        problems = 0
        state_clean = True
        if self.have_bucket:
            left = self.state_list()
            count = sum(bool(line) for line in left.out.splitlines()) if left.ok else 0
            if count:
                print(f"Terraform state still has {count} resource(s).")
                state_clean = False
            else:
                print("Terraform state is empty.")
            checked, lock = self.state_lock()
            if not checked:
                print("Could not check the state lock.")
                state_clean = False
            elif lock:
                print(f"The state is still locked: s3://{self.bucket}/{LOCK_KEY}")
                state_clean = False
            if not state_clean:
                problems += 1
        env = {**os.environ, "AWS_REGION": self.region}
        for variable, value in (("CLUSTER_NAME", self.cluster), ("ALARM_PREFIX", self.alarm_prefix),
                                ("K8S_NAMESPACE", self.namespace), ("INGRESS_NAME", self.ingress)):
            if value:
                env[variable] = value
        if not self.run_logged("6-cleanup-orphans", [str(SCRIPTS / "cleanup-orphans.py"), "--apply"], env=env):
            problems += 1
        if self.ops_instance:
            tunnels = run(["pgrep", "-f", f"ssm start-session.*{self.ops_instance}"], stderr=DROP)
            if tunnels.ok and tunnels.out:
                print("The SSM tunnel to the deleted ops instance is still running (costs nothing): "
                      f"kill {' '.join(tunnels.out.splitlines())} ")

        self.step("7/8 State bucket")
        if not self.have_bucket:
            print("Already gone.")
        elif not state_clean:
            print(f"Kept {self.bucket}: the state still has resources or a lock (see 6/8).")
        else:
            self.run_logged("7-state-bucket", [sys.executable, str(SCRIPTS / "lib/state.py"), "delete", self.bucket])
            status = state_bucket_exists(self.bucket)
            if status == 1:
                print(f"Deleted {self.bucket}.")
            elif status == 2:
                print(f"Could not check that {self.bucket} is gone.")
                problems += 1
            else:
                print(f"The state bucket {self.bucket} is still there. Empty and delete it in the S3 console.")
                problems += 1

        self.step("8/8 Account sweep")
        if not self.run_logged("8-account-sweep", [str(SCRIPTS / "account-sweep.py")]):
            problems += 1
        relative = self.log_dir.relative_to(REPO_ROOT)
        if problems:
            print(f"\nNot clean: {problems} problem(s) above. Logs: {relative}")
            return 1
        print(f"\nClean: the stack is gone and nothing billable is left. Logs: {relative}")
        return 0


def main():
    args = sys.argv[1:]
    if any(arg in ("-h", "--help") for arg in args):
        print(__doc__)
        return 0
    unknown = [arg for arg in args if arg not in ("--yes", "--skip-dump")]
    if unknown:
        print(f"Unknown option: {unknown[0]}\n\n{__doc__}", file=sys.stderr)
        return 1
    try:
        return Teardown("--skip-dump" in args).run()
    except TeardownStopped as error:
        print(f"\nSTOPPED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    entrypoint(main)
