"""Exercise bootstrap with real kubectl and a local, isolated Kubernetes API.

The API implements discovery and Deployment storage only. Inspecting kubectl's
actual three-way patches catches ownership regressions that command mocks miss.
No cluster, credentials, network service, or third-party Python package is used.
"""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest import TestCase, main, skipUnless
import json
import os
import shutil
import subprocess
import sys
import tempfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/apply-workload.py"
KUBECTL = shutil.which("kubectl")
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
COLLECTION = "/apis/apps/v1/namespaces/hospitalsystem/deployments"
RESOURCE = COLLECTION + "/hospital-backend"


def manifest():
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "hospital-backend", "namespace": "hospitalsystem"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "backend"}},
            "template": {
                "metadata": {"labels": {"app": "backend"}},
                "spec": {"containers": [{
                    "name": "backend", "image": "example.test/backend:initial",
                    "imagePullPolicy": "Always",
                    "resources": {"requests": {"cpu": "100m"}},
                }]},
            },
        },
    }


def deployment(replicas=4, image="example.test/backend:release-42"):
    previous = manifest()
    previous["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] = {
        "memory": "512Mi"
    }
    current = deepcopy(previous)
    current["metadata"].update({
        "uid": "deployment-uid", "resourceVersion": "1",
        "annotations": {LAST_APPLIED: json.dumps(previous)},
    })
    current["spec"]["replicas"] = replicas
    current["spec"]["template"]["spec"]["containers"][0]["image"] = image
    return current


def merge_patch(current, patch):
    """Minimal strategic merge storage: maps and containers keyed by name."""
    if isinstance(patch, dict):
        result = deepcopy(current) if isinstance(current, dict) else {}
        for key, value in patch.items():
            if key.startswith("$"):
                continue
            if value is None:
                result.pop(key, None)
            else:
                result[key] = merge_patch(result.get(key), value)
        return result
    if isinstance(patch, list) and all(isinstance(item, dict) and "name" in item for item in patch):
        result = deepcopy(current or [])
        for item in patch:
            index = next((i for i, old in enumerate(result) if old["name"] == item["name"]), None)
            if index is None:
                result.append(deepcopy(item))
            elif item.get("$patch") == "delete":
                result.pop(index)
            else:
                result[index] = merge_patch(result[index], item)
        return result
    return deepcopy(patch)


class DeploymentAPI:
    def __init__(self, live):
        self.live = deepcopy(live)
        self.writes = []
        self.read_error = None
        self.after_read = None
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, body, status=200):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def error(self, status, reason):
                self.respond({"apiVersion": "v1", "kind": "Status", "status": "Failure",
                              "reason": reason, "message": reason, "code": status}, status)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                group = {"name": "apps", "versions": [{"groupVersion": "apps/v1", "version": "v1"}],
                         "preferredVersion": {"groupVersion": "apps/v1", "version": "v1"}}
                if path == "/api":
                    self.respond({"apiVersion": "v1", "kind": "APIVersions", "versions": ["v1"]})
                elif path == "/apis":
                    self.respond({"apiVersion": "v1", "kind": "APIGroupList", "groups": [group]})
                elif path in ("/api/v1", "/apis/apps/v1"):
                    resources = [] if path == "/api/v1" else [{
                        "name": "deployments", "singularName": "deployment", "namespaced": True,
                        "kind": "Deployment", "verbs": ["get", "list", "create", "patch"],
                    }]
                    self.respond({"apiVersion": "v1", "kind": "APIResourceList",
                                  "groupVersion": "v1" if path == "/api/v1" else "apps/v1",
                                  "resources": resources})
                elif path == RESOURCE:
                    if api.read_error:
                        self.error(403, api.read_error)
                        return
                    if api.live is None:
                        self.error(404, "NotFound")
                    else:
                        self.respond(api.live)
                    if api.after_read:
                        callback, api.after_read = api.after_read, None
                        callback(api)
                else:
                    self.error(404, "NotFound")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.writes.append(("create", deepcopy(body)))
                if self.path.split("?", 1)[0] != COLLECTION:
                    self.error(404, "NotFound")
                elif api.live is not None:
                    self.error(409, "AlreadyExists")
                else:
                    api.live = body
                    api.live["metadata"].update({"uid": "new-uid", "resourceVersion": "1"})
                    api.live["spec"].setdefault("replicas", 1)
                    self.respond(api.live, 201)

            def do_PATCH(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                kind = "json" if self.headers["Content-Type"] == "application/json-patch+json" else "apply"
                api.writes.append((kind, deepcopy(body)))
                if kind == "json":
                    updated = deepcopy(api.live)
                    for operation in body:
                        keys = [key.replace("~1", "/").replace("~0", "~") for key in operation["path"].split("/")[1:]]
                        parent = updated
                        for key in keys[:-1]:
                            parent = parent[key]
                        if operation["op"] == "test":
                            if parent[keys[-1]] != operation["value"]:
                                self.error(409, "Conflict")
                                return
                        elif operation["op"] == "replace":
                            parent[keys[-1]] = operation["value"]
                        else:
                            self.error(422, "Unsupported test API operation")
                            return
                    api.live = updated
                else:
                    api.live = merge_patch(api.live, body)
                self.respond(api.live)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@skipUnless(KUBECTL, "kubectl is needed to verify Kubernetes three-way apply")
class BootstrapWorkloadTests(TestCase):
    def start_api(self, live):
        self.api = DeploymentAPI(live)
        self.addCleanup(self.api.close)
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.directory / "kubeconfig.json"
        config.write_text(json.dumps({
            "apiVersion": "v1", "kind": "Config", "current-context": "isolated-test",
            "clusters": [{"name": "test", "cluster": {"server": f"http://127.0.0.1:{self.api.server.server_port}"}}],
            "contexts": [{"name": "isolated-test", "context": {"cluster": "test", "user": "test"}}],
            "users": [{"name": "test", "user": {}}],
        }))
        # Validation needs OpenAPI; disable only in this isolated test wrapper.
        # Real kubectl still computes and sends the actual three-way patch.
        wrapper = self.directory / "kubectl"
        wrapper.write_text(
            f"#!{sys.executable}\nimport subprocess, sys\n"
            f"args = [{KUBECTL!r}, '--kubeconfig', {str(config)!r}, "
            f"'--cache-dir', {str(self.directory / 'cache')!r}, *sys.argv[1:]]\n"
            "if sys.argv[1] in ('create', 'apply'): args.append('--validate=false')\n"
            "sys.exit(subprocess.call(args))\n"
        )
        wrapper.chmod(0o755)

    def run_apply(self, desired=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT)], input=json.dumps(desired or manifest()),
            text=True, capture_output=True, timeout=30,
            env={**os.environ, "PATH": f"{self.directory}:{os.environ['PATH']}"},
        )

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_runtime_fields_unowned(self, document):
        self.assertNotIn("replicas", document.get("spec", {}))
        for container in document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            if container.get("name") == "backend":
                self.assertNotIn("image", container)

    def test_first_install_creates_with_initial_image_and_no_replicas(self):
        self.start_api(None)
        self.assert_success(self.run_apply())
        self.assertEqual([kind for kind, _ in self.api.writes], ["create"])
        created = self.api.writes[0][1]
        self.assertNotIn("replicas", created["spec"])
        self.assertEqual(created["spec"]["template"]["spec"]["containers"][0]["image"], "example.test/backend:initial")
        self.assertNotIn("replicas", json.loads(created["metadata"]["annotations"][LAST_APPLIED])["spec"])

    def test_reapply_preserves_runtime_images_and_scaling_but_updates_config(self):
        for replicas, image in ((0, "example.test/backend:rollback-7"),
                                (5, "example.test/backend@sha256:" + "a" * 64)):
            with self.subTest(replicas=replicas, image=image):
                self.start_api(deployment(replicas, image))
                desired = manifest()
                desired["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "200m"
                self.assert_success(self.run_apply(desired))
                migration, apply = self.api.writes
                self.assertEqual(migration[0], "json")
                self.assertEqual(apply[0], "apply")
                # The migration changes only the annotation; it keeps ownership
                # of old limits so the subsequent apply can remove them.
                migrated = json.loads(migration[1][-1]["value"])
                self.assert_runtime_fields_unowned(migrated)
                self.assertEqual(migrated["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"], {"memory": "512Mi"})
                self.assertEqual([op["op"] for op in migration[1]], ["test", "test", "replace"])
                self.assertEqual(migration[1][0]["path"], "/metadata/uid")
                self.assert_runtime_fields_unowned(apply[1])
                self.assertEqual(self.api.live["spec"]["replicas"], replicas)
                container = self.api.live["spec"]["template"]["spec"]["containers"][0]
                self.assertEqual(container["image"], image)
                self.assertEqual(container["resources"], {"requests": {"cpu": "200m"}})
                self.assert_runtime_fields_unowned(json.loads(self.api.live["metadata"]["annotations"][LAST_APPLIED]))
                self.api.writes.clear()
                self.assert_success(self.run_apply(desired))
                self.assertEqual(self.api.writes, [])

    def test_concurrent_release_is_not_overwritten_by_image_snapshot(self):
        self.start_api(deployment())
        def publish(api):
            api.live["spec"]["template"]["spec"]["containers"][0]["image"] = "example.test/backend:new-release"
            api.live["spec"]["replicas"] = 7
        self.api.after_read = publish
        self.assert_success(self.run_apply())
        self.assertEqual(self.api.live["spec"]["replicas"], 7)
        self.assertEqual(self.api.live["spec"]["template"]["spec"]["containers"][0]["image"], "example.test/backend:new-release")
        for kind, body in self.api.writes:
            if kind == "apply":
                self.assert_runtime_fields_unowned(body)

    def test_existing_workload_without_annotation_keeps_release_and_scale(self):
        live = deployment(0, "example.test/backend:rollback-7")
        del live["metadata"]["annotations"]
        self.start_api(live)
        self.assert_success(self.run_apply())
        self.assertEqual([kind for kind, _ in self.api.writes], ["apply"])
        self.assert_runtime_fields_unowned(self.api.writes[0][1])
        self.assertEqual(self.api.live["spec"]["replicas"], 0)
        self.assertEqual(self.api.live["spec"]["template"]["spec"]["containers"][0]["image"], "example.test/backend:rollback-7")
        self.assert_runtime_fields_unowned(json.loads(self.api.live["metadata"]["annotations"][LAST_APPLIED]))

    def test_concurrent_creation_fails_without_overwriting_release(self):
        self.start_api(None)
        self.api.after_read = lambda api: setattr(api, "live", deployment())
        result = self.run_apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AlreadyExists", result.stderr)
        self.assertEqual([kind for kind, _ in self.api.writes], ["create"])
        self.assertEqual(self.api.live["spec"]["template"]["spec"]["containers"][0]["image"], "example.test/backend:release-42")

    def test_concurrent_annotation_change_or_recreation_aborts_apply(self):
        for changed in ("annotation", "uid"):
            with self.subTest(changed=changed):
                self.start_api(deployment())
                def conflict(api):
                    if changed == "uid":
                        api.live["metadata"]["uid"] = "recreated-uid"
                    else:
                        api.live["metadata"]["annotations"][LAST_APPLIED] = json.dumps(manifest())
                self.api.after_read = conflict
                result = self.run_apply()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Conflict", result.stderr)
                self.assertEqual([kind for kind, _ in self.api.writes], ["json"])

    def test_api_error_aborts_before_any_mutation(self):
        self.start_api(deployment())
        self.api.read_error = "Forbidden"
        result = self.run_apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Forbidden", result.stderr)
        self.assertEqual(self.api.writes, [])

    def test_malformed_deployment_or_annotation_aborts_before_any_mutation(self):
        for broken in ("deployment", "annotation-json", "annotation-shape"):
            with self.subTest(broken=broken):
                live = deployment()
                if broken == "deployment":
                    del live["spec"]["template"]["spec"]["containers"][0]["image"]
                else:
                    live["metadata"]["annotations"][LAST_APPLIED] = "invalid-json" if broken == "annotation-json" else "[]"
                self.start_api(live)
                result = self.run_apply()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.api.writes, [])


if __name__ == "__main__":
    main()
