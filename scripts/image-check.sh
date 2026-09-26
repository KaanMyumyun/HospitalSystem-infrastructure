#!/usr/bin/env bash
#./scripts/image-check.sh            # both apps; changes nothing
#./scripts/image-check.sh backend    # only one of them
# ./scripts/monitoring.sh ecr runs the same check next to the ECR image list.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib/config.sh
source "$SCRIPT_DIR/lib/config.sh"
# shellcheck source=scripts/lib/image-check.sh
source "$SCRIPT_DIR/lib/image-check.sh"

AWS_REGION="${AWS_REGION:-$(group_var aws_region)}"
NAMESPACE="${NAMESPACE:-$(group_var k8s_namespace)}"
BACKEND_DEPLOYMENT="${BACKEND_DEPLOYMENT:-$(group_var backend_deployment)}"
FRONTEND_DEPLOYMENT="${FRONTEND_DEPLOYMENT:-$(group_var frontend_deployment)}"
GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-$(group_var github_repository)}"
KUBE_TIMEOUT="${KUBE_TIMEOUT:-30s}"

usage() {
  cat <<'USAGE'
Usage: scripts/image-check.sh [backend | frontend]

Shows which image each app runs and whether it has the newest code:
  - the image tag on the Deployment, when it was pushed and its other tags
  - whether every pod runs the digest that tag points to in ECR
  - the commit it was built from, taken from its <date>-<sha> tag, compared
    with main on GitHub (needs gh, logged in)

Exits 1 when a check fails: a read error, or pods not running the tagged
image. Code that is older than main, or built with uncommitted changes, is a
warning.

Environment overrides: AWS_REGION, NAMESPACE, BACKEND_DEPLOYMENT,
FRONTEND_DEPLOYMENT, GITHUB_REPOSITORY, KUBE_TIMEOUT (30s), NO_COLOR.
USAGE
}

deployments=()
while [ $# -gt 0 ]; do
  case "$1" in
    backend) deployments+=("$BACKEND_DEPLOYMENT") ;;
    frontend) deployments+=("$FRONTEND_DEPLOYMENT") ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done
[ ${#deployments[@]} -gt 0 ] || deployments=("$BACKEND_DEPLOYMENT" "$FRONTEND_DEPLOYMENT")

for var in AWS_REGION NAMESPACE BACKEND_DEPLOYMENT FRONTEND_DEPLOYMENT; do
  if [ -z "${!var}" ]; then
    printf 'Missing %s: export it, or run ./scripts/tf.sh apply to generate %s\n' \
      "$var" "$GROUP_VARS_DIR/terraform.yml" >&2
    exit 1
  fi
done

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\e[1m' DIM=$'\e[2m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RED=$'\e[31m' RESET=$'\e[0m'
else
  BOLD="" DIM="" GREEN="" YELLOW="" RED="" RESET=""
fi

export AWS_PAGER=""
# The playbooks write the tunnel kubeconfig here and leave ~/.kube/config alone.
export KUBECONFIG="$REPO_ROOT/.generated/kubeconfig"
OKS=0 WARNS=0 FAILS=0 SKIPS=0

note() { printf '%s%s%s\n' "$DIM" "$1" "$RESET"; }
ok() { OKS=$((OKS + 1)); printf '%sOK%s   %s\n' "$GREEN" "$RESET" "$1"; }
warn() { WARNS=$((WARNS + 1)); printf '%sWARN%s %s\n' "$YELLOW" "$RESET" "$1"; }
fail() { FAILS=$((FAILS + 1)); printf '%sFAIL%s %s\n' "$RED" "$RESET" "$1"; }
skip() { SKIPS=$((SKIPS + 1)); printf '%sSKIP%s %s\n' "$DIM" "$RESET" "$1"; }
awsr() { aws --region "$AWS_REGION" "$@"; }
kube() { kubectl --request-timeout="$KUBE_TIMEOUT" "$@"; }
last_line() { printf '%s\n' "$1" | awk 'NF { line = $0 } END { print line }'; }

note "Opening the SSM tunnel to the private EKS API (ansible/playbooks/kubeconfig.yml)..."
if ! tunnel="$(cd "$REPO_ROOT" && ansible-playbook ansible/playbooks/kubeconfig.yml 2>&1)"; then
  tail -n 15 <<<"$tunnel" | sed 's/^/    /'
  printf '%sFAIL%s Could not reach the EKS API through the SSM tunnel\n' "$RED" "$RESET"
  exit 1
fi

for deployment in "${deployments[@]}"; do
  printf '\n%s== %s ==%s\n' "$BOLD" "$deployment" "$RESET"
  check_image "$deployment"
done

printf '\n%d ok, %d warning(s), %d failure(s), %d skipped\n' "$OKS" "$WARNS" "$FAILS" "$SKIPS"
[ "$FAILS" -eq 0 ]
