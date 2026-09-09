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

: "${AWS_REGION:?Set AWS_REGION}"
: "${ECR_REGISTRY:?Set ECR_REGISTRY}"
: "${BACKEND_SOURCE_DIR:?Set BACKEND_SOURCE_DIR}"
: "${FRONTEND_SOURCE_DIR:?Set FRONTEND_SOURCE_DIR}"
: "${BACKEND_DOCKERFILE:?Set BACKEND_DOCKERFILE}"
: "${FRONTEND_DOCKERFILE:?Set FRONTEND_DOCKERFILE}"
: "${BACKEND_REPOSITORY_URL:?Set BACKEND_REPOSITORY_URL}"
: "${FRONTEND_REPOSITORY_URL:?Set FRONTEND_REPOSITORY_URL}"
: "${IMAGE_TAG:?Set IMAGE_TAG}"
: "${FRONTEND_API_URL:?Set FRONTEND_API_URL}"

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

backend_local_image="hospitalsystem-backend-initial:${IMAGE_TAG}"
frontend_local_image="hospitalsystem-frontend-initial:${IMAGE_TAG}"
backend_remote_image="${BACKEND_REPOSITORY_URL}:${IMAGE_TAG}"
frontend_remote_image="${FRONTEND_REPOSITORY_URL}:${IMAGE_TAG}"

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

docker build \
  -t "$backend_local_image" \
  -f "$BACKEND_SOURCE_DIR/$BACKEND_DOCKERFILE" \
  "$BACKEND_SOURCE_DIR"

docker tag "$backend_local_image" "$backend_remote_image"
docker push "$backend_remote_image"

docker build \
  --build-arg "VITE_API_URL=$FRONTEND_API_URL" \
  -t "$frontend_local_image" \
  -f "$FRONTEND_SOURCE_DIR/$FRONTEND_DOCKERFILE" \
  "$FRONTEND_SOURCE_DIR"

docker tag "$frontend_local_image" "$frontend_remote_image"
docker push "$frontend_remote_image"

echo "Pushed initial images:"
echo "$backend_remote_image"
echo "$frontend_remote_image"
