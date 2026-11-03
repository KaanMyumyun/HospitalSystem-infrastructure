#!/usr/bin/env bash
set -euo pipefail

if [ "${PUSH_INITIAL_ECR_IMAGES:-true}" != "true" ]; then
  echo "Initial ECR image push disabled; skipping."
  exit 0
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_command aws
require_command docker
require_command git

: "${AWS_REGION:?Set AWS_REGION}"
: "${ECR_REGISTRY:?Set ECR_REGISTRY}"
: "${BACKEND_SOURCE_DIR:?Set BACKEND_SOURCE_DIR}"
: "${FRONTEND_SOURCE_DIR:?Set FRONTEND_SOURCE_DIR}"
: "${BACKEND_DOCKERFILE:?Set BACKEND_DOCKERFILE}"
: "${FRONTEND_DOCKERFILE:?Set FRONTEND_DOCKERFILE}"
: "${BACKEND_REPOSITORY_URL:?Set BACKEND_REPOSITORY_URL}"
: "${FRONTEND_REPOSITORY_URL:?Set FRONTEND_REPOSITORY_URL}"
: "${IMAGE_TAG:?Set IMAGE_TAG}"

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not available." >&2
  exit 1
fi

if [ ! -d "$BACKEND_SOURCE_DIR" ]; then
  echo "Backend source directory does not exist: $BACKEND_SOURCE_DIR" >&2
  exit 1
fi

if [ ! -d "$FRONTEND_SOURCE_DIR" ]; then
  echo "Frontend source directory does not exist: $FRONTEND_SOURCE_DIR" >&2
  exit 1
fi

if [ ! -f "$BACKEND_SOURCE_DIR/$BACKEND_DOCKERFILE" ]; then
  echo "Backend Dockerfile does not exist: $BACKEND_SOURCE_DIR/$BACKEND_DOCKERFILE" >&2
  exit 1
fi

if [ ! -f "$FRONTEND_SOURCE_DIR/$FRONTEND_DOCKERFILE" ]; then
  echo "Frontend Dockerfile does not exist: $FRONTEND_SOURCE_DIR/$FRONTEND_DOCKERFILE" >&2
  exit 1
fi

source_revision() {
  local dir="$1" rev
  if ! rev="$(git -C "$dir" rev-parse --short=7 HEAD 2>/dev/null)"; then
    echo "unknown"
    return
  fi
  if [ -n "$(git -C "$dir" status --porcelain 2>/dev/null)" ]; then
    rev="${rev}-dirty"
  fi
  echo "$rev"
}

build_date="$(date -u +%Y-%m-%d)"
build_created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

backend_revision="$(source_revision "$BACKEND_SOURCE_DIR")"
frontend_revision="$(source_revision "$FRONTEND_SOURCE_DIR")"
backend_revision_tag="${build_date}-${backend_revision}"
frontend_revision_tag="${build_date}-${frontend_revision}"

image_exists() {
  aws ecr describe-images \
    --region "$AWS_REGION" \
    --repository-name "${1#*/}" \
    --image-ids "imageTag=$2" >/dev/null 2>&1
}

build_and_push() {
  local name="$1" source_dir="$2" dockerfile="$3" repo_url="$4" revision="$5" revision_tag="$6"
  shift 6

  local local_image="hospitalsystem-${name}-initial:${IMAGE_TAG}"
  local remote_image="${repo_url}:${IMAGE_TAG}"
  local revision_image="${repo_url}:${revision_tag}"

  if image_exists "$repo_url" "$IMAGE_TAG"; then
    echo "$remote_image already exists; not rebuilding it."
    return
  fi

  docker build \
    --platform linux/amd64 \
    "$@" \
    --label "org.opencontainers.image.revision=${revision}" \
    --label "org.opencontainers.image.created=${build_created}" \
    -t "$local_image" \
    -f "${source_dir}/${dockerfile}" \
    "$source_dir"

  docker tag "$local_image" "$remote_image"
  docker tag "$local_image" "$revision_image"
  docker push "$remote_image"
  docker push "$revision_image"
  echo "Pushed $remote_image and $revision_image (revision ${revision})"
}

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

build_and_push \
  backend \
  "$BACKEND_SOURCE_DIR" \
  "$BACKEND_DOCKERFILE" \
  "$BACKEND_REPOSITORY_URL" \
  "$backend_revision" \
  "$backend_revision_tag"

build_and_push \
  frontend \
  "$FRONTEND_SOURCE_DIR" \
  "$FRONTEND_DOCKERFILE" \
  "$FRONTEND_REPOSITORY_URL" \
  "$frontend_revision" \
  "$frontend_revision_tag" \
  --build-arg VITE_API_URL=/api
