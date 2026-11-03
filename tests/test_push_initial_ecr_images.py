"""Run the first-image push with fake AWS, Git and Docker commands."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import TestCase, main


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/push-initial-ecr-images.py"
FAKE = r'''
import os, sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(tool + " " + " ".join(args) + "\n")
if tool == "git":
    if "rev-parse" in args:
        print("abcdef0")
    elif "status" in args and os.environ.get("FAKE_DIRTY") == "1":
        print(" M source")
elif tool == "aws":
    if "get-login-password" in args:
        print("fake-login-password")
    elif "describe-images" in args:
        repo = args[args.index("--repository-name") + 1]
        if repo not in os.environ.get("FAKE_EXISTING", "").split(","):
            sys.exit(1)
elif tool == "docker":
    if args[0] == "info" and os.environ.get("FAKE_DOCKER_DOWN") == "1":
        sys.exit(1)
    if args[0] == "login":
        if sys.stdin.read().strip() != "fake-login-password":
            sys.exit(1)
    if args[0] == "build" and os.environ.get("FAKE_BUILD_FAIL") == "1":
        print("build failed", file=sys.stderr)
        sys.exit(2)
'''


class PushInitialImagesTests(TestCase):
    def run_push(self, *, variables=None, missing=()):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name in ("aws", "docker", "git"):
            path = bin_dir / name
            path.write_text(f"#!{sys.executable}\n{FAKE}")
            path.chmod(0o755)
        backend = tmp / "backend"
        frontend = tmp / "frontend"
        backend.mkdir()
        frontend.mkdir()
        (backend / "Dockerfile").write_text("FROM scratch\n")
        (frontend / "Dockerfile").write_text("FROM scratch\n")
        for path in missing:
            (tmp / path).unlink()
        log = tmp / "calls.log"
        log.touch()
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(log),
            "AWS_REGION": "eu-north-1", "ECR_REGISTRY": "registry.example",
            "BACKEND_SOURCE_DIR": str(backend), "FRONTEND_SOURCE_DIR": str(frontend),
            "BACKEND_DOCKERFILE": "Dockerfile", "FRONTEND_DOCKERFILE": "Dockerfile",
            "BACKEND_REPOSITORY_URL": "registry.example/hospital-backend",
            "FRONTEND_REPOSITORY_URL": "registry.example/hospital-frontend", "IMAGE_TAG": "latest",
        }
        env.update(variables or {})
        result = subprocess.run([sys.executable, str(SCRIPT)], env=env,
                                text=True, capture_output=True, timeout=30)
        return result, log.read_text()

    def test_builds_and_pushes_both_images_with_revision_tags(self):
        result, calls = self.run_push(variables={"FAKE_DIRTY": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls.count("docker build"), 2)
        self.assertIn("--build-arg VITE_API_URL=/api", calls)
        self.assertEqual(calls.count("docker tag"), 4)
        self.assertEqual(calls.count("docker push"), 4)
        self.assertIn("-abcdef0-dirty", result.stdout)
        self.assertNotIn("fake-login-password", result.stdout + result.stderr + calls)

    def test_existing_tags_are_not_rebuilt(self):
        result, calls = self.run_push(variables={"FAKE_EXISTING": "hospital-backend,hospital-frontend"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("docker login", calls)
        self.assertNotIn("docker build", calls)
        self.assertIn("already exists; not rebuilding it", result.stdout)

    def test_disabled_push_needs_no_settings_or_tools(self):
        result, calls = self.run_push(variables={"PUSH_INITIAL_ECR_IMAGES": "false", "AWS_REGION": ""})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, "")
        self.assertIn("disabled; skipping", result.stdout)

    def test_missing_dockerfile_stops_before_login(self):
        result, calls = self.run_push(missing=("backend/Dockerfile",))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Backend Dockerfile does not exist", result.stderr)
        self.assertNotIn("get-login-password", calls)

    def test_unavailable_docker_daemon_stops_before_login(self):
        result, calls = self.run_push(variables={"FAKE_DOCKER_DOWN": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Docker daemon is not available", result.stderr)
        self.assertNotIn("get-login-password", calls)

    def test_failed_build_stops_before_tag_or_push(self):
        result, calls = self.run_push(variables={"FAKE_BUILD_FAIL": "1"})
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("docker tag", calls)
        self.assertNotIn("docker push", calls)


if __name__ == "__main__":
    main()
