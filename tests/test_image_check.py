"""Run scripts/image-check.sh against fake kubectl, aws, gh and ansible-playbook.

Every external command must match a fixture, so the tests never contact a
cluster, AWS or GitHub. Overrides come first and replace the healthy answer.
"""

from pathlib import Path
from unittest import TestCase, main
import json
import os
import subprocess
import sys
import tempfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/image-check.sh"
REGISTRY = "123456789012.dkr.ecr.eu-north-1.amazonaws.com"
TAG = "2026-09-26-a9b466a-152"
DIGEST = "sha256:" + "a" * 64
OLD_DIGEST = "sha256:" + "b" * 64
ERROR = {"stderr": "AccessDenied: simulated read failure", "code": 1}

FAKE_TOOL = r'''
import json, os, sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = " ".join(sys.argv[1:])
for route in json.loads(os.environ["FAKE_SCENARIO"]):
    if route["tool"] == tool and all(part in args for part in route["contains"]):
        answer = route["answer"]
        if isinstance(answer, str):
            answer = {"stdout": answer}
        print(answer.get("stdout", ""), end="")
        print(answer.get("stderr", ""), end="", file=sys.stderr)
        sys.exit(answer.get("code", 0))
with open(os.environ["FAKE_UNEXPECTED"], "a") as unexpected:
    unexpected.write(tool + " " + args + "\n")
sys.exit(99)
'''


def route(tool, contains, answer):
    return {"tool": tool, "contains": [contains] if isinstance(contains, str) else contains,
            "answer": answer}


def pods(app, *digests):
    return "".join(f"{app}-{i}\t{REGISTRY}/{app}@{digest}\n" for i, digest in enumerate(digests))


def healthy_routes(tag=TAG, tags=f"latest,{TAG}"):
    routes = [route("ansible-playbook", "kubeconfig.yml", ""), route("gh", "auth status", "")]
    for app in ("hospital-backend", "hospital-frontend"):
        routes += [
            route("kubectl", f"get deployment {app}", f"{REGISTRY}/{app}:{tag}"),
            route("kubectl", ["get pods", f"name={app}"], pods(app, DIGEST, DIGEST)),
            route("aws", ["describe-images", f"--repository-name {app}"],
                  f"{DIGEST}\t2026-09-26T19:37:00.826000+03:00\t{tags}\n"),
        ]
    return routes + [route("gh", "compare/a9b466a...main", "identical\t0\t0\t0\t\t\n")]


class ImageCheckTests(TestCase):
    def run_check(self, *args, overrides=(), routes=None):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for tool in ("aws", "kubectl", "gh", "ansible-playbook"):
            (bin_dir / tool).write_text(f"#!{sys.executable}\n{FAKE_TOOL}")
            (bin_dir / tool).chmod(0o755)
        # An empty group_vars directory, so a live stack's terraform.yml can't leak in.
        (tmp / "group_vars").mkdir()
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp), "NO_COLOR": "1",
            "GROUP_VARS_DIR": str(tmp / "group_vars"),
            "AWS_REGION": "eu-north-1", "NAMESPACE": "hospitalsystem",
            "BACKEND_DEPLOYMENT": "hospital-backend", "FRONTEND_DEPLOYMENT": "hospital-frontend",
            "GITHUB_REPOSITORY": "owner/app",
            "FAKE_SCENARIO": json.dumps([*overrides, *(routes or healthy_routes())]),
            "FAKE_UNEXPECTED": str(tmp / "unexpected"),
        }
        result = subprocess.run(["bash", str(SCRIPT), *args], env=env,
                                capture_output=True, text=True, timeout=30)
        unexpected = tmp / "unexpected"
        self.assertFalse(unexpected.exists(), unexpected.read_text() if unexpected.exists() else "")
        return result

    def assertExit(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def test_current_image_on_the_tip_of_main_passes(self):
        result = self.run_check()
        self.assertExit(result, 0)
        self.assertIn(f"All 2 hospital-backend pod(s) run {TAG}", result.stdout)
        self.assertIn("hospital-frontend was built from a9b466a, the tip of main", result.stdout)
        self.assertIn("4 ok, 0 warning(s), 0 failure(s)", result.stdout)

    def test_one_app_is_checked_alone(self):
        result = self.run_check("frontend")
        self.assertExit(result, 0)
        self.assertNotIn("hospital-backend", result.stdout)

    def test_newer_code_on_main_is_a_warning_naming_the_files(self):
        result = self.run_check(overrides=[
            route("gh", "compare/a9b466a...main", "ahead\t2\t0\t3\tProgram.cs, README.md, app.tsx\tc0ffee1\n"),
        ])
        self.assertExit(result, 0)
        self.assertIn("main (c0ffee1) has 2 newer commit(s) changing 3 file(s): Program.cs, README.md", result.stdout)
        self.assertNotIn("tip of main", result.stdout)

    def test_newer_merges_without_file_changes_are_current(self):
        result = self.run_check(overrides=[route("gh", "compare/a9b466a...main", "ahead\t3\t0\t0\t\tc0ffee1\n")])
        self.assertExit(result, 0)
        self.assertIn("3 commit(s) newer, but none change files", result.stdout)
        self.assertIn("0 warning(s)", result.stdout)

    def test_commit_not_on_main_is_a_warning(self):
        for answer, expected in (("behind\t0\t2\t0\t\t\n", "2 commit(s) ahead of main (not merged yet)"),
                                 ("diverged\t1\t2\t4\tx\tc0ffee1\n", "which isn't on main")):
            with self.subTest(answer=answer):
                result = self.run_check(overrides=[route("gh", "compare/a9b466a...main", answer)])
                self.assertExit(result, 0)
                self.assertIn(expected, result.stdout)

    def test_bootstrap_image_with_uncommitted_changes_is_a_warning(self):
        tag = "2026-09-26-356248d-dirty"
        result = self.run_check(routes=[
            *healthy_routes(tag=tag, tags=f"latest,{tag}"),
            route("gh", "compare/356248d...main", "ahead\t3\t0\t0\t\ta9b466a\n"),
        ])
        self.assertExit(result, 0)
        self.assertIn("built from 356248d plus uncommitted changes", result.stdout)

    def test_latest_resolves_the_commit_from_its_other_tag(self):
        result = self.run_check(routes=healthy_routes(tag="latest"))
        self.assertExit(result, 0)
        self.assertIn("All 2 hospital-backend pod(s) run latest", result.stdout)
        self.assertIn("built from a9b466a, the tip of main", result.stdout)

    def test_image_without_a_commit_tag_is_a_warning(self):
        result = self.run_check(routes=healthy_routes(tag="latest", tags="latest"))
        self.assertExit(result, 0)
        self.assertIn("No tag of hospital-backend's image names the commit", result.stdout)

    def test_pod_on_another_digest_fails(self):
        result = self.run_check(overrides=[
            route("kubectl", ["get pods", "name=hospital-backend"], pods("hospital-backend", DIGEST, OLD_DIGEST)),
        ])
        self.assertExit(result, 1)
        self.assertIn(f"Pod hospital-backend-1 runs {OLD_DIGEST}, but {TAG} now points to {DIGEST}", result.stdout)
        self.assertNotIn("All 2 hospital-backend pod(s)", result.stdout)

    def test_pod_that_has_not_started_is_a_warning(self):
        result = self.run_check(overrides=[
            route("kubectl", ["get pods", "name=hospital-backend"], f"hospital-backend-0\t{REGISTRY}/x@{DIGEST}\nhospital-backend-1\t\n"),
        ])
        self.assertExit(result, 0)
        self.assertIn("Pod hospital-backend-1 hasn't started its container yet", result.stdout)
        self.assertNotIn("All 2 hospital-backend pod(s)", result.stdout)

    def test_read_failures_fail(self):
        for override, expected in (
            (route("kubectl", "get deployment hospital-backend", ERROR), "Could not read the image hospital-backend runs"),
            (route("aws", ["describe-images", "hospital-backend"], ERROR), "Could not find hospital-backend's image"),
            (route("kubectl", ["get pods", "name=hospital-backend"], ERROR), "Could not list the hospital-backend pods"),
            (route("ansible-playbook", "kubeconfig.yml", ERROR), "Could not reach the EKS API"),
        ):
            with self.subTest(expected=expected):
                result = self.run_check(overrides=[override])
                self.assertExit(result, 1)
                self.assertIn(expected, result.stdout)

    def test_github_unavailable_skips_only_the_comparison(self):
        result = self.run_check(overrides=[route("gh", "auth status", {"code": 1})])
        self.assertExit(result, 0)
        self.assertIn("gh is not logged in", result.stdout)
        self.assertIn("All 2 hospital-backend pod(s)", result.stdout)
        result = self.run_check(overrides=[route("gh", "compare/", ERROR)])
        self.assertExit(result, 0)
        self.assertIn("Could not compare hospital-backend's commit a9b466a with main", result.stdout)


if __name__ == "__main__":
    main()
