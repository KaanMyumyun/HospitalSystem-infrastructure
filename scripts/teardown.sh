#!/usr/bin/env bash
#./scripts/teardown.sh         # destroys the stack once you type the cluster name
#./scripts/teardown.sh --yes   # without asking
# Runs every step around ./scripts/tf.sh destroy, then sweeps every region.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib/config.sh
source "$SCRIPT_DIR/lib/config.sh"

# Read now: the destroy deletes terraform.yml early, and the later steps
# still need these.
AWS_REGION="${AWS_REGION:-$(group_var aws_region)}"
AWS_REGION="${AWS_REGION:-eu-north-1}"
ALARM_PREFIX="${ALARM_PREFIX:-$(group_var monitoring_alarm_prefix)}"
K8S_NAMESPACE="${K8S_NAMESPACE:-$(group_var k8s_namespace)}"
INGRESS_NAME="${INGRESS_NAME:-$(group_var ingress_name)}"
TF_DIR="$REPO_ROOT/terraform"
LOCK_FILE="$TF_DIR/.terraform.tfstate.lock.info"
LOG_DIR="$REPO_ROOT/.generated/teardown/$(date -u +%Y%m%dT%H%M%SZ)"
ASSUME_YES=false

usage() {
  cat <<'USAGE'
Usage: scripts/teardown.sh [--yes]

Destroys everything in Terraform state and checks that nothing billable is
left anywhere in the account:

  1. checks: AWS login, the logged-in account is the stack's, no lock left
     by an interrupted Terraform run; then asks you to type the cluster name
  2. destroys terraform_data.kubernetes_cleanup alone, which runs
     cleanup-kubernetes.yml while the cluster can still delete its ALB
  3. scripts/pre-destroy-cleanup.sh --apply for anything step 2 left
     (retried once: AWS can report a just-freed target group as in use)
  4. ./scripts/tf.sh destroy; if it fails, runs step 3 again, deletes the
     EKS cluster security group EKS sometimes leaves in the VPC, and
     retries once
  5. checks the state is empty, then scripts/cleanup-orphans.sh --apply
     for this stack's target groups and ALB alarms
  6. scripts/account-sweep.py: everything billable in every region

Stops at the first step that fails, so a destroy never starts while an ALB
it would stall on is still up. With nothing in state it runs only 5 and 6.
Each step's output is also saved under .generated/teardown/.

  --yes     don't ask for the cluster name
  -h        show this help

Exits 1 when a step fails or the sweep finds something billable.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) ASSUME_YES=true ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

step() {
  printf '\n== %s ==\n' "$1"
}

stop() {
  printf '\nSTOPPED: %s\n' "$1" >&2
  exit 1
}

# run_logged NAME COMMAND...: shows the output and keeps a copy in LOG_DIR.
run_logged() {
  local log="$LOG_DIR/$1.log"
  shift
  printf '(log: %s)\n' "${log#"$REPO_ROOT"/}"
  "$@" 2>&1 | tee "$log"
}

state_list() {
  terraform -chdir="$TF_DIR" state list
}

output() {
  python3 -c 'import json, sys; print(json.load(sys.stdin).get(sys.argv[1], {}).get("value", ""))' "$1" <<<"$OUTPUTS"
}

# Retried once: AWS can report a target group of a just-deleted ALB in use.
pre_destroy_cleanup() {
  if [ -z "$VPC_ID" ]; then
    printf 'No VPC in state, so nothing in it to clean up.\n'
    return 0
  fi
  if run_logged "$1" env VPC_ID="$VPC_ID" "$SCRIPT_DIR/pre-destroy-cleanup.sh" --apply; then
    return 0
  fi
  printf '\nRetrying once.\n'
  run_logged "$1-retry" env VPC_ID="$VPC_ID" "$SCRIPT_DIR/pre-destroy-cleanup.sh" --apply
}

# EKS can fail to delete its cluster security group while a node's ENI still
# holds it, and pre-destroy-cleanup.sh only deletes the k8s-* groups.
delete_cluster_security_group() {
  local ids id
  [ -n "$VPC_ID" ] && [ -n "$CLUSTER_NAME" ] || return 0
  ids="$(aws ec2 describe-security-groups --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=eks-cluster-sg-$CLUSTER_NAME-*" \
    --query 'SecurityGroups[].GroupId' --output text)" || return 1
  for id in $ids; do
    printf 'Deleting EKS cluster security group %s\n' "$id"
    aws ec2 delete-security-group --region "$AWS_REGION" --group-id "$id" || return 1
  done
}

show_what_is_left() {
  printf '\nStill in Terraform state:\n'
  state_list | sed 's/^/  /' || true
  if [ -n "$VPC_ID" ]; then
    printf '\nNetwork interfaces still in %s:\n' "$VPC_ID"
    aws ec2 describe-network-interfaces --region "$AWS_REGION" --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'NetworkInterfaces[].[NetworkInterfaceId, Status, Description]' --output text | sed 's/^/  /' || true
  fi
}

mkdir -p "$LOG_DIR"

step "1/6 Checks"
if ! identity="$(aws sts get-caller-identity --query Account --output text 2>&1)"; then
  stop "No usable AWS credentials ($identity). Run: aws login"
fi
if [ -e "$LOCK_FILE" ]; then
  if pgrep -x terraform >/dev/null; then
    stop "Another Terraform run holds the state lock. Wait for it to finish."
  fi
  stop "A Terraform run was interrupted and left ${LOCK_FILE#"$REPO_ROOT"/}. Nothing is running, so delete it (rm \"$LOCK_FILE\") and run this again."
fi
if ! resources="$(state_list 2>&1)"; then
  stop "Could not read the Terraform state: $resources"
fi
count="$(grep -c . <<<"$resources" || true)"
if ! OUTPUTS="$(terraform -chdir="$TF_DIR" output -json 2>&1)"; then
  stop "Could not read the Terraform outputs: $OUTPUTS"
fi
CLUSTER_NAME="$(output cluster_name)"
VPC_ID="$(output vpc_id)"
OPS_INSTANCE_ID="$(output ops_instance_id)"
state_account="$(output account_id)"
printf 'Account:   %s\nRegion:    %s\nCluster:   %s\nVPC:       %s\nIn state:  %s resource(s)\n' \
  "$identity" "$AWS_REGION" "${CLUSTER_NAME:-none}" "${VPC_ID:-none}" "$count"
if [ -n "$state_account" ] && [ "$state_account" != "$identity" ]; then
  stop "You are logged in to account $identity, but the stack is in $state_account."
fi

if [ "$count" -gt 0 ]; then
  if [ "$ASSUME_YES" = false ]; then
    word="${CLUSTER_NAME:-destroy}"
    printf '\nThis destroys all %s resources and cannot be undone.\n' "$count"
    read -r -p "Type $word to continue: " answer || answer=""
    [ "$answer" = "$word" ] || stop "Not confirmed. Nothing was changed."
  fi

  step "2/6 Kubernetes cleanup while the cluster is up"
  cleanup_address="$(grep -xE 'terraform_data\.kubernetes_cleanup(\[0\])?' <<<"$resources" || true)"
  if [ -z "$cleanup_address" ]; then
    printf 'terraform_data.kubernetes_cleanup is not in state; skipping.\n'
  elif ! run_logged 2-kubernetes-cleanup "$SCRIPT_DIR/tf.sh" destroy -target="$cleanup_address" \
    -auto-approve -input=false -no-color; then
    stop "The Kubernetes cleanup failed, and the cluster is still up. Fix the cause shown above and run this again."
  fi

  step "3/6 Pre-destroy cleanup"
  pre_destroy_cleanup 3-pre-destroy-cleanup \
    || stop "Something blocking the destroy couldn't be deleted (see above). Fix it and run this again."

  step "4/6 terraform destroy"
  if ! run_logged 4-destroy "$SCRIPT_DIR/tf.sh" destroy -auto-approve -input=false -no-color; then
    printf '\nThe destroy failed. Cleaning up what usually blocks it, then retrying once.\n'
    pre_destroy_cleanup 4-pre-destroy-cleanup-again || true
    delete_cluster_security_group || printf 'Could not delete the EKS cluster security group.\n'
    if ! run_logged 4-destroy-retry "$SCRIPT_DIR/tf.sh" destroy -auto-approve -input=false -no-color; then
      show_what_is_left
      stop "The destroy failed twice. The log above says what blocked it."
    fi
  fi
fi

step "5/6 Leftovers"
problems=0
left="$(state_list | grep -c . || true)"
if [ "$left" -gt 0 ]; then
  printf 'Terraform state still has %s resource(s).\n' "$left"
  problems=$((problems + 1))
else
  printf 'Terraform state is empty.\n'
fi
if [ -e "$LOCK_FILE" ]; then
  printf 'The state lock file is still there: %s\n' "$LOCK_FILE"
  problems=$((problems + 1))
fi
if ! run_logged 5-cleanup-orphans env ${CLUSTER_NAME:+CLUSTER_NAME="$CLUSTER_NAME"} \
  ${ALARM_PREFIX:+ALARM_PREFIX="$ALARM_PREFIX"} ${K8S_NAMESPACE:+K8S_NAMESPACE="$K8S_NAMESPACE"} \
  ${INGRESS_NAME:+INGRESS_NAME="$INGRESS_NAME"} AWS_REGION="$AWS_REGION" \
  "$SCRIPT_DIR/cleanup-orphans.sh" --apply; then
  problems=$((problems + 1))
fi
if [ -n "${OPS_INSTANCE_ID:-}" ] && tunnels="$(pgrep -f "ssm start-session.*$OPS_INSTANCE_ID")"; then
  printf 'The SSM tunnel to the deleted ops instance is still running (costs nothing): kill %s\n' \
    "$(tr '\n' ' ' <<<"$tunnels")"
fi

step "6/6 Account sweep"
if ! run_logged 6-account-sweep "$SCRIPT_DIR/account-sweep.py"; then
  problems=$((problems + 1))
fi

if [ "$problems" -gt 0 ]; then
  printf '\nNot clean: %d problem(s) above. Logs: %s\n' "$problems" "${LOG_DIR#"$REPO_ROOT"/}"
  exit 1
fi
printf '\nClean: the stack is gone and nothing billable is left. Logs: %s\n' "${LOG_DIR#"$REPO_ROOT"/}"
