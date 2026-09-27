#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$REPO_ROOT/.env.local"
# shellcheck source=scripts/lib/state.sh
source "$REPO_ROOT/scripts/lib/state.sh"

if [ -f "$env_file" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
else
  echo "Warning: $env_file not found; relying on the existing environment." >&2
fi

required=""
needs_docker=false
needs_ssm_plugin=false
case "${1:-}" in
  apply)
    required="CLOUDFLARE_API_TOKEN HOSPITALSYSTEM_CONNECTION_STRING HOSPITALSYSTEM_JWT_SECRET ALERT_EMAIL"
    needs_docker=true
    needs_ssm_plugin=true
    ;;
  destroy)
    required="CLOUDFLARE_API_TOKEN"
    needs_ssm_plugin=true
    ;;
  plan | refresh | import)
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

if [ -n "${ALERT_EMAIL:-}" ]; then
  export TF_VAR_alert_email="$ALERT_EMAIL"
fi

if [ "$needs_docker" = true ] && ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running; the initial ECR image push will fail." >&2
  echo "Start it with: sudo systemctl start docker" >&2
  exit 1
fi

if [ "$needs_ssm_plugin" = true ] && ! command -v session-manager-plugin >/dev/null 2>&1; then
  echo "session-manager-plugin is not installed; Ansible can't reach the private EKS endpoint." >&2
  echo "Install it: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html" >&2
  exit 1
fi

cd "$REPO_ROOT/terraform"
case "${1:-}" in
  "" | fmt | validate | version | providers | -version | --version | -help | --help | -h)
    exec terraform "$@"
    ;;
esac

# Everything else reads or writes the state in S3.
if ! account="$(aws sts get-caller-identity --query Account --output text)"; then
  echo "No usable AWS credentials. Run: aws login" >&2
  exit 1
fi
bucket="$(state_bucket "$account")"
status=0
state_bucket_exists "$bucket" || status=$?
if [ "$status" = 2 ]; then
  echo "Could not list the S3 buckets (see the error above)." >&2
  exit 1
fi
if [ "$status" = 1 ]; then
  case "$1" in
    apply | plan | init | import)
      echo "Creating the state bucket $bucket (terraform/bootstrap)." >&2
      create_state_bucket "$bucket" >&2
      ;;
    *)
      echo "There is no state bucket ($bucket), so there is no state: nothing has been applied since the last teardown." >&2
      exit 1
      ;;
  esac
fi

# The backend block in versions.tf leaves the bucket out: its name has the
# account ID in it.
if [ "$1" = init ]; then
  exec terraform "$@" -backend-config="bucket=$bucket"
fi
ensure_backend "$bucket" >&2
exec terraform "$@"
