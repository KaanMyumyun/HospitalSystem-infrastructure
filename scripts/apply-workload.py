#!/usr/bin/env python3
"""Apply a Deployment without taking over release images or runtime scaling.

Ansible supplies the rendered manifest as JSON on stdin. Existing workloads
relinquish these fields in client-side apply's last-applied annotation before
the manifest omits them, avoiding a one-time reset of replicas or images.
"""

from copy import deepcopy
import json
import subprocess
import sys


LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def kubectl(*args, document=None):
    result = subprocess.run(
        ["kubectl", *args, "--request-timeout=20s"],
        input=json.dumps(document) if document is not None else None,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def omit_runtime_fields(document, container_names):
    result = deepcopy(document)
    spec = result["spec"]
    spec.pop("replicas", None)
    for container in spec.get("template", {}).get("spec", {}).get("containers", []):
        if container["name"] in container_names:
            container.pop("image", None)
    return result


def apply_workload(manifest):
    if manifest["apiVersion"] != "apps/v1" or manifest["kind"] != "Deployment":
        raise ValueError("Only apps/v1 Deployments are supported")
    name = manifest["metadata"]["name"]
    namespace = manifest["metadata"]["namespace"]
    containers = manifest["spec"]["template"]["spec"]["containers"]
    container_names = {container["name"] for container in containers}
    if not containers or len(container_names) != len(containers):
        raise ValueError("Expected uniquely named application containers")
    if any(not container.get("image") for container in containers):
        raise ValueError("The bootstrap manifest must supply initial images")

    # --ignore-not-found only suppresses NotFound. Auth/network/API errors must
    # stop bootstrap, never fall back to initial images or recreate a workload.
    output = kubectl("get", "deployment", name, "-n", namespace,
                     "--ignore-not-found=true", "-o", "json")
    manifest = deepcopy(manifest)
    manifest["spec"].pop("replicas", None)
    if not output.strip():
        # create fails if another writer creates the Deployment after our read;
        # apply could overwrite that writer's release with the initial image.
        print(kubectl("create", "--save-config", "-f", "-", document=manifest), end="")
        return

    live = json.loads(output)
    if (live["kind"] != "Deployment" or live["metadata"]["name"] != name
            or live["metadata"]["namespace"] != namespace):
        raise ValueError("Kubernetes returned an unexpected Deployment")
    live_containers = {
        container["name"]: container.get("image")
        for container in live["spec"]["template"]["spec"]["containers"]
    }
    if any(not live_containers.get(name) for name in container_names):
        raise ValueError("An existing application container is missing its image")

    previous_text = live["metadata"].get("annotations", {}).get(LAST_APPLIED)
    if previous_text is not None:
        previous = json.loads(previous_text)
        retained = omit_runtime_fields(previous, container_names)
        if retained != previous:
            annotation_path = "/metadata/annotations/" + LAST_APPLIED.replace("/", "~1")
            patch = [
                {"op": "test", "path": "/metadata/uid", "value": live["metadata"]["uid"]},
                {"op": "test", "path": annotation_path, "value": previous_text},
                {"op": "replace", "path": annotation_path, "value": json.dumps(retained)},
            ]
            # Only metadata changes here. Guard against a concurrent apply or
            # recreation without conflicting with HPA/status updates.
            print(kubectl("patch", "deployment", name, "-n", namespace,
                          "--type=json", "--patch-file=/dev/stdin", document=patch), end="")

    # Omit images instead of copying a snapshot: a concurrent CI deployment or
    # rollback can change them between our read and apply without being undone.
    manifest = omit_runtime_fields(manifest, container_names)
    print(kubectl("apply", "-f", "-", document=manifest), end="")


def main():
    try:
        apply_workload(json.load(sys.stdin))
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        return 1
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        print(f"Cannot safely apply Deployment: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
