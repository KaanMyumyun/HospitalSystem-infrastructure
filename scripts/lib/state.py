"""The short-lived S3 bucket that holds Terraform's state.

Both tf.py and teardown.py use the same bucket naming and backend checks.
Terraform still owns creation and deletion of the bucket in terraform/bootstrap.
"""

import json
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.commands import Result, run
from lib.config import REPO_ROOT

BOOTSTRAP_DIR = REPO_ROOT / "terraform/bootstrap"
TF_DIR = REPO_ROOT / "terraform"


def state_bucket(account):
    return f"hospitalsystem-tfstate-{account}"


def state_bucket_exists(bucket):
    """0 if the bucket exists, 1 if absent, 2 if the list call failed."""
    found = run(["aws", "s3api", "list-buckets", "--query", f"Buckets[?Name=='{bucket}'].Name",
                 "--output", "text"], stderr=None)
    if not found.ok:
        return 2
    return 0 if found.out == bucket else 1


def backend_bucket():
    """The bucket local Terraform init last configured, if any."""
    path = TF_DIR / ".terraform/terraform.tfstate"
    try:
        backend = json.loads(path.read_text(encoding="utf-8")).get("backend") or {}
    except FileNotFoundError:
        return ""
    if backend.get("type") == "s3":
        return (backend.get("config") or {}).get("bucket") or ""
    return ""


def ensure_backend(bucket):
    """Run terraform init when its backend points at a different bucket."""
    current = backend_bucket()
    if current == bucket:
        return Result(0, "")
    command = ["terraform", f"-chdir={TF_DIR}", "init", "-input=false", f"-backend-config=bucket={bucket}"]
    if current:
        command.append("-reconfigure")
    return run(command)


def bootstrap(action, bucket, *, stdout=None):
    """Initialize and apply/destroy the state bucket with Terraform.

    stdin stays attached so an apply can ask for approval. The init command's
    normal output is hidden, as it was in state.sh; errors stay visible.
    """
    init = subprocess.run(["terraform", f"-chdir={BOOTSTRAP_DIR}", "init", "-input=false"],
                          stdout=subprocess.DEVNULL, stderr=stdout)
    if init.returncode:
        return init.returncode
    args = ["terraform", f"-chdir={BOOTSTRAP_DIR}", action]
    if action == "destroy":
        args += ["-auto-approve", "-input=false"]
    args += ["-var", f"bucket_name={bucket}"]
    return subprocess.run(args, stdout=stdout, stderr=stdout).returncode


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "delete":
        print("Usage: state.py delete BUCKET", file=sys.stderr)
        return 1
    return bootstrap("destroy", sys.argv[2])


if __name__ == "__main__":
    # The helper also serves as the logged bucket-deletion command in teardown.py.
    sys.exit(main())
