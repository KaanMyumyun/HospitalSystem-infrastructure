#!/usr/bin/env python3
"""Show which image each app runs and whether it has the newest code:

  - the image tag on the Deployment, when it was pushed and its other tags
  - whether every pod runs the digest that tag points to in ECR
  - the commit it was built from, taken from its <date>-<sha> tag, compared
    with main on GitHub (needs gh, logged in)

./scripts/image-check.py            # both apps; changes nothing
./scripts/image-check.py backend    # only one of them

Exits 1 when a check fails: a read error, or pods not running the tagged
image. Code that is older than main, or built with uncommitted changes, is a
warning. ./scripts/monitoring.py ecr runs the same check next to the ECR image
list.

Environment overrides: AWS_REGION, NAMESPACE, BACKEND_DEPLOYMENT,
FRONTEND_DEPLOYMENT, GITHUB_REPOSITORY, KUBE_TIMEOUT (30s), NO_COLOR.
"""

import argparse
import os

from lib.checks import Checks
from lib.commands import ScriptError, entrypoint, print_tail
from lib.config import REPO_ROOT, group_vars_dir, setting
from lib.images import check_image


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("apps", nargs="*", choices=("backend", "frontend"), help="the apps to check (default: both)")
    args = parser.parse_args()

    settings = {
        "AWS_REGION": setting("AWS_REGION", "aws_region"),
        "NAMESPACE": setting("NAMESPACE", "k8s_namespace"),
        "BACKEND_DEPLOYMENT": setting("BACKEND_DEPLOYMENT", "backend_deployment"),
        "FRONTEND_DEPLOYMENT": setting("FRONTEND_DEPLOYMENT", "frontend_deployment"),
    }
    for variable, value in settings.items():
        if not value:
            raise ScriptError(f"Missing {variable}: export it, or run ./scripts/tf.py apply to generate "
                              f"{group_vars_dir()}/terraform.yml")
    apps = {"backend": settings["BACKEND_DEPLOYMENT"], "frontend": settings["FRONTEND_DEPLOYMENT"]}
    deployments = [apps[app] for app in args.apps or ("backend", "frontend")]

    os.environ["AWS_PAGER"] = ""
    # The playbooks write the tunnel kubeconfig here and leave ~/.kube/config alone.
    os.environ["KUBECONFIG"] = str(REPO_ROOT / ".generated/kubeconfig")
    checks = Checks(settings["AWS_REGION"], setting("KUBE_TIMEOUT", default="30s"))

    checks.note("Opening the SSM tunnel to the private EKS API (ansible/playbooks/kubeconfig.yml)...")
    tunnel = checks.ansible("ansible/playbooks/kubeconfig.yml")
    if not tunnel.ok:
        print_tail(tunnel.out)
        checks.fail("Could not reach the EKS API through the SSM tunnel")
        return 1

    github_repository = setting("GITHUB_REPOSITORY", "github_repository")
    for deployment in deployments:
        checks.section(deployment)
        check_image(checks, deployment, settings["NAMESPACE"], github_repository)

    print(f"\n{checks.count('OK')} ok, {checks.count('WARN')} warning(s), "
          f"{checks.count('FAIL')} failure(s), {checks.count('SKIP')} skipped")
    return 1 if checks.count("FAIL") else 0


if __name__ == "__main__":
    entrypoint(main)
