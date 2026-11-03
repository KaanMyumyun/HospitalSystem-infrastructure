#!/usr/bin/env python3
"""Build and push the first backend and frontend images during Terraform apply.

Terraform supplies the paths, repository URLs, AWS region and image tag through
the environment. An existing image tag is left alone, as on later applies.
"""

from datetime import datetime, timezone
import os
from pathlib import Path

from lib.commands import DROP, SHOW, ScriptError, entrypoint, require_command, run


def source_revision(directory):
    head = run(["git", "-C", directory, "rev-parse", "--short=7", "HEAD"], stderr=DROP)
    if not head.ok:
        return "unknown"
    status = run(["git", "-C", directory, "status", "--porcelain"], stderr=DROP)
    return head.out + ("-dirty" if status.out else "")


def image_exists(region, repo_url, tag):
    _, separator, repo = repo_url.partition("/")
    repo = repo if separator else repo_url
    return run(["aws", "ecr", "describe-images", "--region", region, "--repository-name", repo,
                "--image-ids", f"imageTag={tag}"], stderr=DROP).ok


def build_and_push(name, source, dockerfile, repo_url, revision, revision_tag, image_tag, created, region,
                   *build_args):
    local_image = f"hospitalsystem-{name}-initial:{image_tag}"
    remote_image = f"{repo_url}:{image_tag}"
    revision_image = f"{repo_url}:{revision_tag}"
    if image_exists(region, repo_url, image_tag):
        print(f"{remote_image} already exists; not rebuilding it.")
        return
    run(["docker", "build", "--platform", "linux/amd64", *build_args,
         "--label", f"org.opencontainers.image.revision={revision}",
         "--label", f"org.opencontainers.image.created={created}", "-t", local_image,
         "-f", str(source / dockerfile), str(source)], capture=False, stderr=SHOW, check=True)
    for image in (remote_image, revision_image):
        run(["docker", "tag", local_image, image], capture=False, stderr=SHOW, check=True)
    for image in (remote_image, revision_image):
        run(["docker", "push", image], capture=False, stderr=SHOW, check=True)
    print(f"Pushed {remote_image} and {revision_image} (revision {revision})")


def main():
    if os.environ.get("PUSH_INITIAL_ECR_IMAGES", "true") != "true":
        print("Initial ECR image push disabled; skipping.")
        return 0
    for tool in ("aws", "docker", "git"):
        require_command(tool)
    names = ("AWS_REGION", "ECR_REGISTRY", "BACKEND_SOURCE_DIR", "FRONTEND_SOURCE_DIR", "BACKEND_DOCKERFILE",
             "FRONTEND_DOCKERFILE", "BACKEND_REPOSITORY_URL", "FRONTEND_REPOSITORY_URL", "IMAGE_TAG")
    settings = {}
    for name in names:
        value = os.environ.get(name, "")
        if not value:
            raise ScriptError(f"{name}: Set {name}")
        settings[name] = value
    if not run(["docker", "info"], stderr=DROP).ok:
        raise ScriptError("Docker daemon is not available.")
    backend = Path(settings["BACKEND_SOURCE_DIR"])
    frontend = Path(settings["FRONTEND_SOURCE_DIR"])
    for label, source, dockerfile in (("Backend", backend, settings["BACKEND_DOCKERFILE"]),
                                      ("Frontend", frontend, settings["FRONTEND_DOCKERFILE"])):
        if not source.is_dir():
            raise ScriptError(f"{label} source directory does not exist: {source}")
        if not (source / dockerfile).is_file():
            raise ScriptError(f"{label} Dockerfile does not exist: {source / dockerfile}")

    now = datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-%d")
    created = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    backend_revision = source_revision(str(backend))
    frontend_revision = source_revision(str(frontend))
    password = run(["aws", "ecr", "get-login-password", "--region", settings["AWS_REGION"]], stderr=SHOW)
    if not password.ok:
        return password.code
    run(["docker", "login", "--username", "AWS", "--password-stdin", settings["ECR_REGISTRY"]],
        input=password.out + "\n", capture=False, stderr=SHOW, check=True)

    build_and_push("backend", backend, settings["BACKEND_DOCKERFILE"], settings["BACKEND_REPOSITORY_URL"],
                   backend_revision, f"{date}-{backend_revision}", settings["IMAGE_TAG"], created,
                   settings["AWS_REGION"])
    build_and_push("frontend", frontend, settings["FRONTEND_DOCKERFILE"], settings["FRONTEND_REPOSITORY_URL"],
                   frontend_revision, f"{date}-{frontend_revision}", settings["IMAGE_TAG"], created,
                   settings["AWS_REGION"], "--build-arg", "VITE_API_URL=/api")
    return 0


if __name__ == "__main__":
    entrypoint(main)
