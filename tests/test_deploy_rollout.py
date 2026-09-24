"""Run scripts/deploy-release.py against a fake cluster and ECR, without AWS."""

from importlib.util import module_from_spec, spec_from_file_location
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch
import ast
import json
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy-release.py"
SPEC = spec_from_file_location("deploy_release", SCRIPT)
deploy = module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)

REPOSITORIES = {
    name: f"123456789012.dkr.ecr.eu-north-1.amazonaws.com/{name}" for name in deploy.WORKLOADS
}
OLD = {name: f"{url}:old" for name, url in REPOSITORIES.items()}
NEW = {name: f"{url}:new" for name, url in REPOSITORIES.items()}
BACKEND, FRONTEND = "hospital-backend", "hospital-frontend"


def clock():
    return SimpleNamespace(monotonic=Mock(side_effect=count()), sleep=Mock())


class FakeCluster:
    """Deployments that roll out at once, unless their image is broken."""

    def __init__(self, replicas=1, images=OLD, broken=(), failing_patches=(), smoke_failures=None):
        self.replicas = replicas
        self.images = dict(images)
        self.generations = {name: 7 for name in images}
        self.broken = set(broken)
        self.failing_patches = set(failing_patches)
        self.smoke_failures = dict(smoke_failures or {})
        self.patches = []
        self.smoke_requests = []

    def deployment(self, name, patch=None):
        if patch is not None:
            [container] = patch["spec"]["template"]["spec"]["containers"]
            self.check_container(name, container)
            if container["image"] in self.failing_patches:
                raise deploy.DeployError(f"PATCH {name}: HTTP 500")
            self.patches.append((name, container["image"]))
            if container["image"] != self.images[name]:
                self.images[name] = container["image"]
                self.generations[name] += 1
        replicas = self.replicas
        if self.images[name] in self.broken:
            status = {"updatedReplicas": 1, "replicas": replicas + 1, "availableReplicas": replicas,
                      "conditions": [{"reason": "ProgressDeadlineExceeded"}]}
        else:
            status = {"updatedReplicas": replicas, "replicas": replicas, "availableReplicas": replicas}
        status["observedGeneration"] = self.generations[name]
        containers = [{"name": deploy.WORKLOADS[name][0], "image": self.images[name]}]
        return {
            "metadata": {"name": name, "generation": self.generations[name]},
            "spec": {"replicas": replicas, "template": {"spec": {"containers": containers}}},
            "status": status,
        }

    @staticmethod
    def check_container(name, container):
        if container["name"] != deploy.WORKLOADS[name][0]:
            raise AssertionError(f"{name} was patched with container {container['name']}")

    def get_through_service(self, service, path):
        self.smoke_requests.append((service, path))
        if self.smoke_failures.get(service, 0):
            self.smoke_failures[service] -= 1
            raise deploy.DeployError(f"GET {service}: HTTP 503")


class ReleaseTests(TestCase):
    def release(self, cluster, missing=()):
        def find_image(repository, tag):
            if repository in missing:
                raise deploy.DeployError(f"{repository}:{tag} is not in ECR")
            return "sha256:" + "0" * 64

        self.clock = clock()
        with patch("builtins.print"):
            deploy.release(cluster, "new", REPOSITORIES, find_image, self.clock)

    def test_release_updates_both_and_smoke_tests_them(self):
        cluster = FakeCluster()
        self.release(cluster)
        self.assertEqual(cluster.patches, [(BACKEND, NEW[BACKEND]), (FRONTEND, NEW[FRONTEND])])
        self.assertEqual(cluster.smoke_requests, [(BACKEND, "/health/ready"), (FRONTEND, "/")])

    def test_missing_image_changes_nothing(self):
        cluster = FakeCluster()
        with self.assertRaisesRegex(deploy.DeployError, "hospital-frontend:new is not in ECR"):
            self.release(cluster, missing={REPOSITORIES[FRONTEND]})
        self.assertEqual(cluster.patches, [])

    def test_failed_rollout_rolls_back_both(self):
        cluster = FakeCluster(broken={NEW[FRONTEND]})
        with self.assertRaisesRegex(deploy.DeployError, "progress deadline.*Rolled back to .*:old, .*:old"):
            self.release(cluster)
        self.assertEqual(cluster.images, OLD)
        self.assertEqual(cluster.smoke_requests, [])

    def test_failed_smoke_test_rolls_back_both(self):
        cluster = FakeCluster(smoke_failures={BACKEND: deploy.SMOKE_ATTEMPTS})
        with self.assertRaisesRegex(deploy.DeployError, "smoke test GET /health/ready failed.*Rolled back"):
            self.release(cluster)
        self.assertEqual(cluster.images, OLD)
        self.assertEqual(len(cluster.smoke_requests), deploy.SMOKE_ATTEMPTS)

    def test_smoke_test_retries_a_slow_start(self):
        cluster = FakeCluster(smoke_failures={FRONTEND: deploy.SMOKE_ATTEMPTS - 1})
        self.release(cluster)
        self.assertEqual(cluster.images, NEW)
        self.clock.sleep.assert_called_with(5)

    def test_failed_second_patch_rolls_back_the_first(self):
        cluster = FakeCluster(failing_patches={NEW[FRONTEND]})
        with self.assertRaisesRegex(deploy.DeployError, "PATCH hospital-frontend: HTTP 500.*Rolled back"):
            self.release(cluster)
        self.assertEqual(cluster.images, OLD)
        self.assertEqual(cluster.patches[:2], [(BACKEND, NEW[BACKEND]), (BACKEND, OLD[BACKEND])])

    def test_unexpected_api_response_still_rolls_back(self):
        cluster = FakeCluster()
        real = cluster.deployment
        reads = count()

        def deployment(name, patch=None):
            # Reads 0 and 1 record the previous images; read 2 is the first
            # rollout check after both patches.
            if patch is None and next(reads) == 2:
                return {"spec": {}}
            return real(name, patch)

        cluster.deployment = deployment
        with self.assertRaisesRegex(deploy.DeployError, "failed: 'metadata'.*Rolled back"):
            self.release(cluster)
        self.assertEqual(cluster.images, OLD)

    def test_failed_rollback_names_the_previous_images(self):
        cluster = FakeCluster(broken={NEW[FRONTEND], OLD[FRONTEND]})
        with self.assertRaisesRegex(deploy.DeployError, "rollback failed too.*by hand: hospital-backend=.*:old, hospital-frontend=.*:old"):
            self.release(cluster)

    def test_redeploying_the_running_tag_has_nothing_to_roll_back_to(self):
        cluster = FakeCluster(images=NEW, smoke_failures={FRONTEND: deploy.SMOKE_ATTEMPTS})
        with self.assertRaisesRegex(deploy.DeployError, "nothing to roll back to"):
            self.release(cluster)
        self.assertEqual(cluster.patches, [(BACKEND, NEW[BACKEND]), (FRONTEND, NEW[FRONTEND])])

    def test_scaled_to_zero_only_sets_images(self):
        cluster = FakeCluster(replicas=0)
        self.release(cluster)
        self.assertEqual(cluster.images, NEW)
        self.assertEqual(cluster.smoke_requests, [])
        self.clock.sleep.assert_not_called()


def deployment(observed, failed=False):
    return {
        "metadata": {"generation": 12},
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": observed,
            "updatedReplicas": 1,
            "replicas": 1,
            "availableReplicas": 1,
            "conditions": [{"reason": "ProgressDeadlineExceeded"}] if failed else [],
        },
    }


class RolloutTests(TestCase):
    def wait(self, responses, times=(0, 1, 2)):
        self.kube = SimpleNamespace(deployment=Mock(side_effect=responses))
        self.clock = SimpleNamespace(monotonic=Mock(side_effect=times), sleep=Mock())
        with patch("builtins.print"):
            deploy.wait_for_rollouts(self.kube, [BACKEND], deploy.ROLLOUT_SECONDS, self.clock)

    def test_stale_failure_waits_for_corrected_release(self):
        self.wait([deployment(11, failed=True), deployment(12)])
        self.assertEqual(self.kube.deployment.call_count, 2)
        self.clock.sleep.assert_called_once_with(5)

    def test_current_generation_failure_is_reported(self):
        with self.assertRaisesRegex(deploy.DeployError, "exceeded its progress deadline"):
            self.wait([deployment(12, failed=True)])
        self.clock.sleep.assert_not_called()

    def test_stale_status_still_times_out(self):
        with self.assertRaisesRegex(deploy.DeployError, "did not finish within 320 seconds"):
            self.wait([deployment(11, failed=True)], times=(0, 321))

    def test_current_success_finishes_without_waiting(self):
        self.wait([deployment(12)])
        self.assertEqual(self.kube.deployment.call_count, 1)
        self.clock.sleep.assert_not_called()


class ImageCheckTests(TestCase):
    def find(self, returncode, stdout="", stderr=""):
        result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with patch.object(deploy.subprocess, "run", return_value=result) as run:
            try:
                return deploy.ecr_image_finder("eu-north-1", "https://vpce.example")(REPOSITORIES[BACKEND], "new")
            finally:
                self.argv = run.call_args.args[0]

    def test_found_image_returns_its_digest(self):
        self.assertEqual(self.find(0, "sha256:abc\n"), "sha256:abc")
        self.assertIn("--endpoint-url", self.argv)
        self.assertIn("imageTag=new", self.argv)
        self.assertEqual(self.argv[self.argv.index("--repository-name") + 1], BACKEND)

    def test_missing_image_says_so(self):
        with self.assertRaisesRegex(deploy.DeployError, "hospital-backend:new is not in ECR"):
            self.find(254, stderr="An error occurred (ImageNotFoundException) when calling DescribeImages")

    def test_other_ecr_errors_are_not_reported_as_missing(self):
        with self.assertRaisesRegex(deploy.DeployError, "could not look up .*AccessDenied"):
            self.find(254, stderr="An error occurred (AccessDeniedException)")


class WiringTests(TestCase):
    SOURCE = SCRIPT.read_text()

    def test_script_has_no_ssm_parameter_braces(self):
        # SSM would substitute or reject them inside the deploy document.
        self.assertNotIn("{{", self.SOURCE)
        self.assertNotIn("}}", self.SOURCE)

    def test_script_parses_as_python_39(self):
        # Amazon Linux 2023, the ops instance's OS, ships Python 3.9.
        ast.parse(self.SOURCE, feature_version=(3, 9))

    def test_document_sets_every_variable_the_script_reads(self):
        ops = (ROOT / "terraform/ops.tf").read_text()
        block = ops[ops.index("  deploy_settings = {"):ops.index("\n  }\n", ops.index("  deploy_settings = {"))]
        exported = set(re.findall(r"^    ([A-Z0-9_]+)\s+=", block, re.MULTILINE)) | {"IMAGE_TAG"}
        self.assertEqual(set(re.findall(r'env\["([A-Z0-9_]+)"\]', self.SOURCE)), exported)
        self.assertIn("export IMAGE_TAG='{{ ImageTag }}'", ops)
        self.assertIn('file("${path.module}/../scripts/deploy-release.py")', ops)

    def test_rbac_allows_exactly_the_smoke_test_proxies(self):
        kube = object.__new__(deploy.Kubernetes)
        kube.namespace = "hospitalsystem"
        kube.request = Mock()
        for name, (_, path) in deploy.WORKLOADS.items():
            kube.get_through_service(name, path)
        proxied = {re.search(r"/services/([^/]+)/proxy", c.args[1]).group(1) for c in kube.request.call_args_list}
        rbac = (ROOT / "kubernetes/rbac/github-actions-deploy.yaml.j2").read_text()
        allowed = set(re.findall(r'^      - "([^"]+:http)"$', rbac, re.MULTILINE))
        self.assertEqual(proxied, allowed)

    def test_rbac_allows_exactly_what_a_release_does_to_deployments(self):
        cluster = FakeCluster()
        calls = []

        def request(method, path, body=None, timeout=30):
            calls.append((method, path))
            if "/proxy/" in path:
                return b""
            return json.dumps(cluster.deployment(path.rsplit("/", 1)[1], body)).encode()

        kube = object.__new__(deploy.Kubernetes)
        kube.namespace = "hospitalsystem"
        kube.request = request
        with patch("builtins.print"):
            deploy.release(kube, "new", REPOSITORIES, lambda repository, tag: "sha256:0", clock())
        prefix = "/apis/apps/v1/namespaces/hospitalsystem/deployments/"
        used = {(method.lower(), path[len(prefix):]) for method, path in calls if "/proxy/" not in path}
        self.assertTrue(all(path.startswith(prefix) for method, path in calls if "/proxy/" not in path))

        rbac = (ROOT / "kubernetes/rbac/github-actions-deploy.yaml.j2").read_text()
        group_vars = (ROOT / "ansible/group_vars/all/main.yml").read_text()
        names = dict(re.findall(r"^(\w+_deployment): (\S+)$", group_vars, re.MULTILINE))
        self.assertEqual(rbac.count("- deployments"), 1)
        rule = re.search(r"^      - deployments\n    resourceNames:\n((?:      - .+\n)+)    verbs:\n((?:      - .+\n)+)",
                         rbac, re.MULTILINE)
        allowed_names = {names[var] for var in re.findall(r'"\{\{ (\w+) \}\}"', rule.group(1))}
        allowed_verbs = set(re.findall(r"- (\w+)", rule.group(2)))
        self.assertEqual({(verb, name) for verb in allowed_verbs for name in allowed_names}, used)


if __name__ == "__main__":
    main()
