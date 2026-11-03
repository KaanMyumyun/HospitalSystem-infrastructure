#!/usr/bin/env python3
"""Run Terraform with the account's short-lived state bucket and .env.local.

Usage: scripts/python/tf.py TERRAFORM_COMMAND [ARG ...]
The wrapper creates the state bucket before the first apply, plan, init or
import. It uses the active AWS account to name the bucket and initializes the
backend for that bucket before running Terraform.
"""

import os
from pathlib import Path
import shutil
import sys

from lib.commands import DROP, SHOW, ScriptError, entrypoint, run
from lib.config import REPO_ROOT
from lib.env import load_env
from lib.state import bootstrap, ensure_backend, state_bucket, state_bucket_exists

TF_DIR = REPO_ROOT / "terraform"
OFFLINE = {"", "fmt", "validate", "version", "providers", "-version", "--version", "-help", "--help", "-h"}


def terraform(args):
    """Replace the wrapper with Terraform, retaining its exit code and TTY."""
    try:
        os.execvp("terraform", ["terraform", *args])
    except FileNotFoundError:
        print("terraform: command not found", file=sys.stderr)
        return 127


def main():
    args = sys.argv[1:]
    command = args[0] if args else ""
    env_file = REPO_ROOT / ".env.local"
    if env_file.is_file():
        loaded = load_env(env_file)
        os.environ.clear()
        os.environ.update(loaded)
    else:
        print(f"Warning: {env_file} not found; relying on the existing environment.", file=sys.stderr)

    required = {
        "apply": ("CLOUDFLARE_API_TOKEN", "HOSPITALSYSTEM_DB_PASSWORD", "HOSPITALSYSTEM_JWT_SECRET", "ALERT_EMAIL"),
        "destroy": ("CLOUDFLARE_API_TOKEN",),
        "plan": ("CLOUDFLARE_API_TOKEN", "HOSPITALSYSTEM_DB_PASSWORD"),
        "refresh": ("CLOUDFLARE_API_TOKEN", "HOSPITALSYSTEM_DB_PASSWORD"),
        "import": ("CLOUDFLARE_API_TOKEN", "HOSPITALSYSTEM_DB_PASSWORD"),
    }.get(command, ())
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(f"Missing required variables for 'terraform {command}': {' '.join(missing)}", file=sys.stderr)
        print(f"Set them in {env_file} or export them before running this script.", file=sys.stderr)
        return 1

    for source, target in (("CLOUDFLARE_API_TOKEN", "TF_VAR_cloudflare_api_token"),
                           ("ALERT_EMAIL", "TF_VAR_alert_email"),
                           ("HOSPITALSYSTEM_DB_PASSWORD", "TF_VAR_db_password")):
        if os.environ.get(source):
            os.environ[target] = os.environ[source]

    if command == "apply" and not run(["docker", "info"], stderr=DROP).ok:
        print("Docker daemon is not running; the initial ECR image push will fail.", file=sys.stderr)
        print("Start it with: sudo systemctl start docker", file=sys.stderr)
        return 1
    if command in ("apply", "destroy") and not shutil.which("session-manager-plugin"):
        print("session-manager-plugin is not installed; Ansible can't reach the private EKS endpoint.",
              file=sys.stderr)
        print("Install it: https://docs.aws.amazon.com/systems-manager/latest/userguide/"
              "session-manager-working-with-install-plugin.html", file=sys.stderr)
        return 1

    os.chdir(TF_DIR)
    if command in OFFLINE:
        return terraform(args)

    account = run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
                  stderr=SHOW)
    if not account.ok:
        print("No usable AWS credentials. Run: aws login", file=sys.stderr)
        return 1
    bucket = state_bucket(account.out)
    status = state_bucket_exists(bucket)
    if status == 2:
        print("Could not list the S3 buckets (see the error above).", file=sys.stderr)
        return 1
    if status == 1:
        if command in ("apply", "plan", "init", "import"):
            print(f"No state bucket yet: terraform/bootstrap will create {bucket} once you approve it.",
                  file=sys.stderr)
            if bootstrap("apply", bucket, stdout=sys.stderr):
                print(f"The state bucket wasn't created, so terraform {command} didn't run.", file=sys.stderr)
                return 1
        else:
            print(f"There is no state bucket ({bucket}), so there is no state: "
                  "nothing has been applied since the last teardown.", file=sys.stderr)
            return 1

    if command == "init":
        return terraform([*args, f"-backend-config=bucket={bucket}"])
    backend = ensure_backend(bucket)
    if backend.out:
        print(backend.out, file=sys.stderr)
    if not backend.ok:
        return backend.code
    return terraform(args)


if __name__ == "__main__":
    entrypoint(main)
