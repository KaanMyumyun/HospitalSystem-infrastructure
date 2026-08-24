#!/usr/bin/env bash
set -Eeuo pipefail

NAMESPACE="${NAMESPACE:-hospitalsystem}"
AWS_REGION="${AWS_REGION:-eu-north-1}"
EKS_CLUSTER_NAME="${EKS_CLUSTER_NAME:-eks-pr1}"
BACKEND_DEPLOYMENT="${BACKEND_DEPLOYMENT:-hospital-backend}"
FRONTEND_DEPLOYMENT="${FRONTEND_DEPLOYMENT:-hospital-frontend}"
INGRESS_NAME="${INGRESS_NAME:-hospital-ingress}"
LOG_TAIL_LINES="${LOG_TAIL_LINES:-100}"
RUN_ANSIBLE_PLAYBOOKS="${RUN_ANSIBLE_PLAYBOOKS:-true}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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
