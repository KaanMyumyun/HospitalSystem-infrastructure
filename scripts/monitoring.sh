#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GROUP_VARS_DIR="$REPO_ROOT/ansible/group_vars/all"

# Reads a plain top-level value from the Ansible group_vars, including the
# Terraform-generated terraform.yml.
group_var() {
  sed -nE "s/^\"?$1\"?:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/p" \
    "$GROUP_VARS_DIR/main.yml" "$GROUP_VARS_DIR/terraform.yml" 2>/dev/null | tail -n 1 || true
}

NAMESPACE="${NAMESPACE:-$(group_var k8s_namespace)}"
AWS_REGION="${AWS_REGION:-$(group_var aws_region)}"
EKS_CLUSTER_NAME="${EKS_CLUSTER_NAME:-$(group_var eks_cluster_name)}"
BACKEND_DEPLOYMENT="${BACKEND_DEPLOYMENT:-$(group_var backend_deployment)}"
FRONTEND_DEPLOYMENT="${FRONTEND_DEPLOYMENT:-$(group_var frontend_deployment)}"
INGRESS_NAME="${INGRESS_NAME:-$(group_var ingress_name)}"
LOG_TAIL_LINES="${LOG_TAIL_LINES:-100}"
RUN_ANSIBLE_PLAYBOOKS="${RUN_ANSIBLE_PLAYBOOKS:-true}"

section() {
  printf '\n== %s ==\n' "$1"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

has_command() {
  command -v "$1" >/dev/null 2>&1
}

run_optional() {
  local description="$1"
  shift

  if ! "$@"; then
    printf 'Warning: %s failed\n' "$description" >&2
  fi
}

require_command aws
require_command kubectl

for var in NAMESPACE AWS_REGION EKS_CLUSTER_NAME BACKEND_DEPLOYMENT FRONTEND_DEPLOYMENT INGRESS_NAME; do
  if [[ -z "${!var}" ]]; then
    printf 'Missing %s: export it, or run ./scripts/tf.sh apply to generate %s\n' \
      "$var" "$GROUP_VARS_DIR/terraform.yml" >&2
    exit 1
  fi
done

if [[ "$RUN_ANSIBLE_PLAYBOOKS" == "true" ]]; then
  if has_command ansible-playbook; then
    section "Ansible Monitoring Status"
    (
      cd "$REPO_ROOT"
      run_optional "Ansible monitoring status playbook" \
        ansible-playbook ansible/playbooks/monitoring-status.yml
    )

    section "Ansible Application Logs"
    (
      cd "$REPO_ROOT"
      run_optional "Ansible logs playbook" \
        ansible-playbook ansible/playbooks/logs.yml
    )
  else
    printf 'Warning: ansible-playbook is not installed; skipping Ansible checks\n' >&2
  fi
fi

section "Kubeconfig"
aws eks update-kubeconfig --region "$AWS_REGION" --name "$EKS_CLUSTER_NAME" >/dev/null
kubectl config current-context

section "Namespace"
kubectl get namespace "$NAMESPACE"

section "Deployments"
kubectl get deployment "$BACKEND_DEPLOYMENT" "$FRONTEND_DEPLOYMENT" \
  -n "$NAMESPACE" \
  -o wide

section "Pods"
kubectl get pods -n "$NAMESPACE" -o wide

section "Horizontal Pod Autoscalers"
kubectl get hpa -n "$NAMESPACE" -o wide

section "Pod Resource Usage"
run_optional "pod metrics lookup" kubectl top pods -n "$NAMESPACE"

section "Ingress"
kubectl get ingress "$INGRESS_NAME" -n "$NAMESPACE" -o wide

section "Recent Backend Logs"
run_optional "backend log lookup" kubectl logs \
  -n "$NAMESPACE" \
  deployment/"$BACKEND_DEPLOYMENT" \
  --all-containers \
  --prefix \
  --tail="$LOG_TAIL_LINES"

section "Recent Frontend Logs"
run_optional "frontend log lookup" kubectl logs \
  -n "$NAMESPACE" \
  deployment/"$FRONTEND_DEPLOYMENT" \
  --all-containers \
  --prefix \
  --tail="$LOG_TAIL_LINES"

section "Recent Warning Events"
run_optional "event lookup" kubectl get events \
  -n "$NAMESPACE" \
  --field-selector type=Warning \
  --sort-by=.lastTimestamp
