
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

missing=()
for var in CLOUDFLARE_API_TOKEN HOSPITALSYSTEM_CONNECTION_STRING HOSPITALSYSTEM_JWT_SECRET; do
  [ -n "${!var:-}" ] || missing+=("$var")
done

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing required variables: ${missing[*]}" >&2
  echo "Set them in $env_file or export them before running this script." >&2
  exit 1
fi
export TF_VAR_cloudflare_api_token="$CLOUDFLARE_API_TOKEN"

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running; the initial ECR image push will fail." >&2
  echo "Start it with: sudo systemctl start docker" >&2
  exit 1
fi

cd "$repo_root/terraform"
exec terraform "$@"
