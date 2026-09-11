#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$repo_root/.env.local"

if [ -f "$env_file" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
else
  echo "Warning: $env_file not found; relying on the existing environment." >&2
fi

# Only apply builds images and runs the Ansible bootstrap, so only apply needs
# Docker and the app secrets. Commands that call the providers need the
# Cloudflare token; read-only commands (output, state, validate, ...) need none.
required=""
needs_docker=false
case "${1:-}" in
  apply)
    required="CLOUDFLARE_API_TOKEN HOSPITALSYSTEM_CONNECTION_STRING HOSPITALSYSTEM_JWT_SECRET"
    needs_docker=true
    ;;
  plan | destroy | refresh | import)
    required="CLOUDFLARE_API_TOKEN"
    ;;
esac

missing=()
for var in $required; do
  [ -n "${!var:-}" ] || missing+=("$var")
done

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing required variables for 'terraform $1': ${missing[*]}" >&2
  echo "Set them in $env_file or export them before running this script." >&2
  exit 1
fi

if [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then
  export TF_VAR_cloudflare_api_token="$CLOUDFLARE_API_TOKEN"
fi

if [ "$needs_docker" = true ] && ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running; the initial ECR image push will fail." >&2
  echo "Start it with: sudo systemctl start docker" >&2
  exit 1
fi

cd "$repo_root/terraform"
exec terraform "$@"
