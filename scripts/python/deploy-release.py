"""Deploy one image tag to the backend and frontend, and undo it if it fails.

The deploy SSM document (terraform/ops.tf) runs this on the ops instance with
IMAGE_TAG and the cluster settings in the environment. The instance has no
kubectl, so this calls the Kubernetes API directly, and its python3 is 3.9.

1. Check that both images exist in ECR. If either is missing, change nothing.
2. Set both Deployment images and wait for both rollouts.
3. Smoke test each Service through the API server: the backend's
   /health/ready, which also checks the database, and the frontend's index.
4. If step 2 or 3 fails, put back both previous images and wait for that
   rollout too.

SSM reads two braces in a row as a document parameter, so this file must never
contain them; the tests check this.
"""

import base64
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request


# The SSM document times out after 600 seconds: rollout, smoke test and
# rollback have to fit in that together.
ROLLOUT_SECONDS = 320
ROLLBACK_SECONDS = 200
SMOKE_ATTEMPTS = 3

# Deployment (and Service) name: container, smoke test path.
WORKLOADS = {
    "hospital-backend": ("backend", "/health/ready"),
    "hospital-frontend": ("frontend", "/"),
}


class DeployError(Exception):
    pass


class Kubernetes:
    def __init__(self, server, namespace, ca, token):
        self.server = server
        self.namespace = namespace
        self.token = token
        self.context = ssl.create_default_context(cadata=ca)

    def request(self, method, path, body=None, timeout=30):
        request = urllib.request.Request(self.server + path, method=method)
        request.add_header("Authorization", "Bearer " + self.token)
        if body is not None:
            request.data = json.dumps(body).encode()
            request.add_header("Content-Type", "application/strategic-merge-patch+json")
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            raise DeployError(f"{method} {path}: HTTP {error.code}: {detail}") from None
        except OSError as error:
            raise DeployError(f"{method} {path}: {error}") from None

    def deployment(self, name, patch=None):
        path = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}"
        body = self.request("GET" if patch is None else "PATCH", path, patch)
        try:
            return json.loads(body)
        except ValueError:
            raise DeployError(f"{name}: the API returned a Deployment that isn't JSON") from None

    def get_through_service(self, service, path):
        # The Services name their port "http"; the RBAC Role allows exactly
        # these "<service>:http" proxy names.
        self.request("GET", f"/api/v1/namespaces/{self.namespace}/services/{service}:http/proxy{path}", timeout=10)


def run_aws(*args):
    try:
        result = subprocess.run(["aws", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeployError(f"aws {args[0]} {args[1]}: {error}") from None
    return result


def eks_token(region, cluster):
    result = run_aws("eks", "get-token", "--region", region, "--cluster-name", cluster,
                     "--query", "status.token", "--output", "text")
    if result.returncode != 0:
        raise DeployError(f"could not get an EKS token: {result.stderr.strip()}")
    return result.stdout.strip()


def ecr_image_finder(region, endpoint):
    def find_image(repository_url, tag):
        repository = repository_url.split("/", 1)[1]
        result = run_aws("ecr", "describe-images", "--region", region, "--endpoint-url", endpoint,
                         "--repository-name", repository, "--image-ids", f"imageTag={tag}",
                         "--query", "imageDetails[0].imageDigest", "--output", "text")
        if result.returncode == 0 and result.stdout.startswith("sha256:"):
            return result.stdout.strip()
        if "ImageNotFoundException" in result.stderr:
            raise DeployError(f"{repository}:{tag} is not in ECR")
        raise DeployError(f"could not look up {repository}:{tag} in ECR: {result.stderr.strip()}")
    return find_image


def container_image(deployment, name):
    container = WORKLOADS[name][0]
    for spec in deployment["spec"]["template"]["spec"]["containers"]:
        if spec["name"] == container:
            return spec["image"]
    raise DeployError(f"{name} has no {container} container")


def set_images(kube, images):
    """Patch each Deployment's image and return the names that run pods."""
    running = []
    for name, image in images.items():
        container = {"name": WORKLOADS[name][0], "image": image}
        deployment = kube.deployment(name, {"spec": {"template": {"spec": {"containers": [container]} } } })
        print(f"{name}: image set to {image}")
        if deployment["spec"].get("replicas", 0) > 0:
            running.append(name)
    return running


def wait_for_rollouts(kube, names, seconds, clock):
    deadline = clock.monotonic() + seconds
    for name in names:
        while True:
            deployment = kube.deployment(name)
            want = deployment["spec"].get("replicas", 0)
            status = deployment.get("status", {})
            # Until the controller has seen this generation, the status and its
            # conditions can still describe an earlier, failed rollout.
            observed = status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
            if observed and any(c.get("reason") == "ProgressDeadlineExceeded" for c in status.get("conditions", [])):
                raise DeployError(f"{name}: rollout exceeded its progress deadline")
            if (
                observed
                and status.get("updatedReplicas", 0) == want
                and status.get("replicas", 0) == want
                and status.get("availableReplicas", 0) == want
            ):
                print(f"{name}: rollout complete")
                break
            if clock.monotonic() > deadline:
                raise DeployError(f"{name}: rollout did not finish within {seconds} seconds")
            clock.sleep(5)


def smoke_test(kube, names, clock):
    for name in names:
        path = WORKLOADS[name][1]
        for attempt in range(1, SMOKE_ATTEMPTS + 1):
            try:
                kube.get_through_service(name, path)
                break
            except DeployError as error:
                if attempt == SMOKE_ATTEMPTS:
                    raise DeployError(f"{name}: smoke test GET {path} failed: {error}") from None
                clock.sleep(5)
        print(f"{name}: smoke test GET {path} passed")


def roll_back(kube, previous, target, clock):
    changed = {name: image for name, image in previous.items() if image != target[name]}
    if not changed:
        return "Both Deployments already ran this tag before the deploy, so there was nothing to roll back to."
    try:
        running = set_images(kube, changed)
        wait_for_rollouts(kube, running, ROLLBACK_SECONDS, clock)
    except Exception as error:
        images = ", ".join(f"{name}={image}" for name, image in changed.items())
        return (f"The rollback failed too ({error}). Restore the previous images by hand: {images}")
    return "Rolled back to " + ", ".join(changed.values()) + "."


def release(kube, tag, repositories, find_image, clock=time):
    for name in WORKLOADS:
        digest = find_image(repositories[name], tag)
        print(f"{repositories[name]}:{tag} is in ECR ({digest})")

    previous = {name: container_image(kube.deployment(name), name) for name in WORKLOADS}
    target = {name: f"{repositories[name]}:{tag}" for name in WORKLOADS}
    try:
        running = set_images(kube, target)
        if not running:
            print(f"Both Deployments are scaled to 0. The next scale-up runs {tag}.")
            return
        wait_for_rollouts(kube, running, ROLLOUT_SECONDS, clock)
        smoke_test(kube, running, clock)
    # Anything after the first patch, even an unexpected API response, must
    # not leave the two workloads on different releases.
    except Exception as error:
        print(f"Deploy of {tag} failed: {error}. Rolling back.")
        raise DeployError(f"Deploy of {tag} failed: {error}. {roll_back(kube, previous, target, clock)}") from None
    print(f"Deploy of {tag} complete")


def main():
    env = os.environ
    try:
        kube = Kubernetes(
            env["EKS_SERVER"],
            env["K8S_NAMESPACE"],
            base64.b64decode(env["EKS_CA"]).decode(),
            eks_token(env["AWS_REGION"], env["EKS_CLUSTER"]),
        )
        repositories = {
            "hospital-backend": env["BACKEND_REPOSITORY"],
            "hospital-frontend": env["FRONTEND_REPOSITORY"],
        }
        release(kube, env["IMAGE_TAG"], repositories, ecr_image_finder(env["AWS_REGION"], env["ECR_ENDPOINT"]))
    except DeployError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
