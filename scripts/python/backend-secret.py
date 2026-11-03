#!/usr/bin/env python3
"""Synchronize backend secrets and acknowledge only completed Deployment rollouts.

Only Secret metadata is read; values never enter arguments, output or files.
The pod template hash is a restart request. The Deployment metadata hash is
an acknowledgement, written separately after the rollout succeeds.
"""

import argparse
import json
import subprocess
import sys
import time
import uuid


SECRET_HASH = "reconcile.external-secrets.io/data-hash"
STARTED_WITH = "hospitalsystem.io/backend-secrets-hash"


class ReconcileError(RuntimeError):
    """An operation failed without exposing raw command output."""


def kubectl(*args, document=None):
    try:
        result = subprocess.run(
            ["kubectl", *args, "--request-timeout=20s"],
            input=json.dumps(document) if document is not None else None,
            text=True, capture_output=True, check=True, timeout=330,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # API errors can include resource bodies. Never echo raw output.
        raise ReconcileError(f"kubectl {' '.join(args[:2])} failed; no hash was acknowledged") from exc
    return result.stdout


def annotation(document, name):
    return (document.get("metadata", {}).get("annotations") or {}).get(name, "")


def ready(document):
    return any(condition.get("type") == "Ready" and condition.get("status") == "True"
               for condition in document.get("status", {}).get("conditions", []))


def fresh_sync(document, previous_version, token):
    status = document.get("status", {})
    return (annotation(document, "force-sync") == token and ready(document)
            and bool(status.get("refreshTime"))
            and bool(status.get("syncedResourceVersion"))
            and status["syncedResourceVersion"] != previous_version)


def wait_for(probe, description, attempts, delay):
    for attempt in range(attempts):
        result = probe()
        if result:
            return result
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise ReconcileError(f"Timed out waiting for {description}; no hash was acknowledged")


def sync_secret(namespace, name, attempts, delay):
    token = uuid.uuid4().hex
    # The mutation response gives an atomic baseline, not a pre-annotation
    # read which a concurrent reconciliation could already have superseded.
    requested = json.loads(kubectl(
        "annotate", "externalsecret", name, "-n", namespace, "--overwrite",
        f"force-sync={token}", "-o", "json",
    ))
    previous_version = requested.get("status", {}).get("syncedResourceVersion", "")

    def synced():
        current = json.loads(kubectl("get", "externalsecret", name, "-n", namespace, "-o", "json"))
        if annotation(current, "force-sync") != token:
            raise ReconcileError("Another secret sync replaced this request; run again")
        return fresh_sync(current, previous_version, token)

    # ESO 2.11 writes Ready, refreshTime and syncedResourceVersion together
    # with Status().Update. An older reconcile conflicts with our annotation
    # update and must requeue, so it cannot acknowledge this new request.
    # Unlike refreshTime alone, the version also distinguishes same-second
    # refreshes. The data hash need not change when the stored values do not.
    wait_for(synced, "a fresh ExternalSecret sync", attempts, delay)


def secret_hash(namespace, name):
    path = SECRET_HASH.replace(".", r"\.")
    return kubectl("get", "secret", name, "-n", namespace, "--ignore-not-found=true",
                   "-o", f"jsonpath={{.metadata.annotations.{path}}}").strip()


def deployment(namespace, name):
    output = kubectl("get", "deployment", name, "-n", namespace,
                     "--ignore-not-found=true", "-o", "json")
    return json.loads(output) if output.strip() else None


def rollout_complete(live):
    desired = live["spec"].get("replicas", 1)
    status = live.get("status", {})
    return (status.get("observedGeneration", 0) >= live["metadata"]["generation"]
            and all(status.get(key, 0) == desired for key in
                    ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")))


def reconcile_backend(namespace, name, secret, attempts, delay):
    current_hash = wait_for(lambda: secret_hash(namespace, secret), "the Secret's data hash", attempts, delay)
    live = deployment(namespace, name)
    if live is None:
        return {"changed": False, "restarted": False, "deployment_exists": False}

    uid = live["metadata"]["uid"]
    acknowledged = annotation(live, STARTED_WITH)
    template = live["spec"]["template"]
    restarted = False
    template_hash = annotation(template, STARTED_WITH)
    needs_restart = acknowledged != current_hash or (template_hash and template_hash != current_hash)
    if needs_restart and template_hash != current_hash:
        annotations = dict(template.get("metadata", {}).get("annotations") or {})
        annotations[STARTED_WITH] = current_hash
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "test", "path": "/metadata/generation", "value": live["metadata"]["generation"]},
            {"op": "add", "path": "/spec/template/metadata/annotations", "value": annotations},
        ]
        live = json.loads(kubectl("patch", "deployment", name, "-n", namespace,
                                  "--type=json", "--patch-file=/dev/stdin", "-o", "json", document=patch))
        restarted = True

    # Keep paused workloads paused. kubectl rollout status may wait forever
    # for a Deployment at zero; poll its observed generation and zero counts.
    if live["spec"].get("replicas", 1):
        kubectl("rollout", "status", f"deployment/{name}", "-n", namespace, "--timeout=300s")

    expected_template = live["spec"]["template"]

    def completed():
        current = deployment(namespace, name)
        if (current is None or current["metadata"]["uid"] != uid
                or current["spec"]["template"] != expected_template):
            raise ReconcileError("The backend changed during secret rollout; run again")
        return current if rollout_complete(current) else None

    live = wait_for(completed, "the backend rollout", attempts, delay)
    if secret_hash(namespace, secret) != current_hash:
        raise ReconcileError("The Secret changed during backend rollout; run again")
    if annotation(live, STARTED_WITH) != current_hash:
        # Test the entire resource version: recreation, another restart or
        # metadata writer must never receive an acknowledgement for our pods.
        annotations = dict(live["metadata"].get("annotations") or {})
        annotations[STARTED_WITH] = current_hash
        patch = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": live["metadata"]["resourceVersion"]},
            {"op": "add", "path": "/metadata/annotations", "value": annotations},
        ]
        kubectl("patch", "deployment", name, "-n", namespace, "--type=json",
                "--patch-file=/dev/stdin", document=patch)
    return {"changed": restarted or acknowledged != current_hash,
            "restarted": restarted, "deployment_exists": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--secret", default="hospital-backend-secrets")
    parser.add_argument("--reconcile-only", action="store_true",
                        help="Reconcile pods with the already synced Secret after applying workloads")
    parser.add_argument("--attempts", type=int, default=36)
    parser.add_argument("--delay", type=float, default=5)
    args = parser.parse_args()
    if args.attempts < 1 or args.delay < 0:
        parser.error("--attempts must be positive and --delay must be nonnegative")
    try:
        if not args.reconcile_only:
            sync_secret(args.namespace, args.secret, args.attempts, args.delay)
        result = reconcile_backend(args.namespace, args.deployment, args.secret, args.attempts, args.delay)
        result["changed"] = result["changed"] or not args.reconcile_only
        print(json.dumps(result))
    except (ReconcileError, OSError, KeyError, TypeError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ReconcileError) else "Invalid response or unavailable command"
        print(f"Cannot reconcile backend secrets: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
