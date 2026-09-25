#!/usr/bin/env python3
"""Rotate the backend's JWT signing key.

.env.local is the source of truth: every rebuild stores its values in Secrets
Manager again, so the new key goes into .env.local before anything that can
fail, and a key changed only in Secrets Manager would be lost.

1. Generate a random key and save it to .env.local, mode 0600. Only the
   HOSPITALSYSTEM_JWT_SECRET line changes.
2. With the stack down, stop: the next ./scripts/tf.sh apply stores it.
3. With the stack up, load .env.local the way tf.sh does and run
   backend-secret.yml, which stores the key, syncs the Secret and restarts
   the backend.
4. Check that the backend's pods started with the new Secret and are ready.

The backend accepts one key at a time, so every signed-in user is logged out.
The key is never printed or passed on a command line.
"""

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time


KEY_VARIABLE = "HOSPITALSYSTEM_JWT_SECRET"
# 64 random bytes as 86 URL-safe characters, which need no shell quoting. The
# app signs with HmacSha256, which needs a key of at least 32 bytes.
KEY_BYTES = 64
KEY_LINE = re.compile(rf"[ \t]*(?:export[ \t]+)?{KEY_VARIABLE}=")
TOOLS = ("ansible-playbook", "kubectl", "aws", "session-manager-plugin")

PLAYBOOK = "ansible/playbooks/backend-secret.yml"
# The subshell keeps the values out of the caller's shell.
PUSH_AGAIN = f"(set -a; source .env.local; ansible-playbook {PLAYBOOK})"
TERRAFORM_VARS = "ansible/group_vars/all/terraform.yml"
ANSIBLE_VARS = "ansible/group_vars/all/main.yml"
KUBECONFIG = ".generated/kubeconfig"

# Same names as backend-secret.yml: External Secrets hashes the Secret's data
# into one annotation, the playbook records the hash the pods started with.
SECRET = "hospital-backend-secrets"
SECRET_HASH = r"{.metadata.annotations.reconcile\.external-secrets\.io/data-hash}"
STARTED_WITH = "hospitalsystem.io/backend-secrets-hash"
# An HPA scale-up right after the restart can leave a pod briefly unready.
CHECK_ATTEMPTS = 12


class RotationError(Exception):
    pass


def load_env(env_file):
    """Return the environment tf.sh runs with after sourcing env_file."""
    script = (
        'set -euo pipefail; set -a; source "$1"; set +a; '
        'exec "$2" -c "import json, os, sys; json.dump(dict(os.environ), sys.stdout)"'
    )
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(env_file), sys.executable],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RotationError(f"could not source {env_file}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def save_key(env_file, key):
    """Put key on env_file's HOSPITALSYSTEM_JWT_SECRET line, or add that line.

    Every other line stays byte for byte the same. The file is replaced in
    one rename, so it never holds half a write, and ends up mode 0600.
    """
    path = env_file.resolve()
    with open(path, encoding="utf-8", newline="") as source:
        lines = source.read().splitlines(keepends=True)
    found = False
    for index, line in enumerate(lines):
        match = KEY_LINE.match(line)
        if match:
            ending = line[len(line.rstrip("\r\n")):]
            lines[index] = match.group(0) + key + ending
            found = True
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{KEY_VARIABLE}={key}\n")

    # mkstemp creates the file 0600, and its name matches .gitignore's .env.*
    # if a crash leaves it behind.
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            out.writelines(lines)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def yaml_value(path, name):
    """Read a top-level scalar from a flat vars file, quoted or not."""
    pattern = re.compile(rf"""^["']?{name}["']?:[ \t]*["']?([^"'\s#]+)""", re.M)
    match = pattern.search(path.read_text(encoding="utf-8"))
    if not match:
        raise RotationError(f"{path} has no {name}")
    return match.group(1)


def kubectl(root, *args):
    command = ["kubectl", "--kubeconfig", str(root / KUBECONFIG), "--request-timeout=20s", *args]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RotationError(f"kubectl {' '.join(args[:3])} failed: {result.stderr.strip()}")
    return result.stdout


def backend_problem(deployment, secret_hash):
    """Say what is still wrong with the backend, or None when it's done."""
    name = deployment["metadata"]["name"]
    started_with = deployment["metadata"].get("annotations", {}).get(STARTED_WITH, "")
    if not secret_hash or started_with != secret_hash:
        return (f"{name} didn't record the new Secret's hash, so its pods may "
                "still use the old key")
    want = deployment["spec"].get("replicas", 1)
    status = deployment.get("status", {})
    # Until the controller has seen this generation, its status can still
    # describe the pods from before the restart.
    observed = status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
    updated = status.get("updatedReplicas", 0)
    ready = status.get("readyReplicas", 0)
    if want and not (observed and updated == want and ready == want and status.get("replicas", 0) == want):
        return f"{name} has {updated} of {want} pods on the new Secret and {ready} ready"
    return None


def check_backend(root):
    namespace = yaml_value(root / TERRAFORM_VARS, "k8s_namespace")
    name = yaml_value(root / ANSIBLE_VARS, "backend_deployment")
    delay = float(os.environ.get("RETRY_DELAY", "5"))
    for attempt in range(CHECK_ATTEMPTS):
        if attempt:
            time.sleep(delay)
        secret_hash = kubectl(root, "get", "secret", SECRET, "-n", namespace, "-o", f"jsonpath={SECRET_HASH}")
        deployment = json.loads(kubectl(root, "get", "deployment", name, "-n", namespace, "-o", "json"))
        problem = backend_problem(deployment, secret_hash.strip())
        if problem is None:
            break
    else:
        raise RotationError(f"{problem}. Check with ./scripts/monitoring.sh workloads, "
                            f"and push the saved key again with: {PUSH_AGAIN}")
    want = deployment["spec"].get("replicas", 1)
    if not want:
        return f"{name} is scaled to 0; its next scale-up reads the new key."
    return f"{name} restarted with the new key: {want} of {want} pods ready."


def confirm(question):
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        print()
        return False


def rotate(root, assume_yes):
    env_file = root / ".env.local"
    if not env_file.is_file():
        raise RotationError(f"{env_file} not found. Copy .env.example to .env.local first.")
    stack_up = (root / TERRAFORM_VARS).is_file()
    if stack_up:
        missing = [tool for tool in TOOLS if shutil.which(tool) is None]
        if missing:
            raise RotationError(f"the stack is up, but {', '.join(missing)} isn't installed")
        # backend-secret.yml stores new values only when it gets both.
        if not load_env(env_file).get("HOSPITALSYSTEM_CONNECTION_STRING"):
            raise RotationError("the stack is up, but HOSPITALSYSTEM_CONNECTION_STRING "
                                f"isn't set in {env_file}")

    if not assume_yes and not confirm("Every signed-in user will be logged out. Continue? [y/N] "):
        print("Nothing changed.")
        return 1

    key = secrets.token_urlsafe(KEY_BYTES)
    save_key(env_file, key)
    env = load_env(env_file)
    if env.get(KEY_VARIABLE) != key:
        raise RotationError(f"saved the new key, but sourcing {env_file} gives a different "
                            f"{KEY_VARIABLE}. Check what else sets it.")
    print(f"Saved a new JWT key to {env_file}.")
    if not stack_up:
        print("The stack is down. The next ./scripts/tf.sh apply stores the key.")
        return 0

    print(f"Pushing it with {PLAYBOOK}.", flush=True)
    if subprocess.run(["ansible-playbook", PLAYBOOK], cwd=root, env=env).returncode != 0:
        raise RotationError(f"{PLAYBOOK} failed, so the backend may still use the old key. "
                            f"The new key is saved; push it again from {root} with: {PUSH_AGAIN}")
    print(check_backend(root))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate a new JWT signing key, save it to .env.local and, "
                    "when the stack is up, restart the backend with it.")
    parser.add_argument("--yes", action="store_true",
                        help="don't ask before logging every user out")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="repository root (default: the one this script is in)")
    args = parser.parse_args(argv)
    try:
        return rotate(args.root.resolve(), args.yes)
    except RotationError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
