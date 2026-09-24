#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GROUP_VARS_DIR="$REPO_ROOT/ansible/group_vars/all"

group_var() {
  sed -nE "s/^\"?$1\"?:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/p" \
    "$GROUP_VARS_DIR/main.yml" "$GROUP_VARS_DIR/terraform.yml" 2>/dev/null | tail -n 1 || true
}

# terraform.yml is gone after a destroy, so these fall back to the fixed names
# in terraform/locals.tf, terraform/variables.tf and group_vars/all/main.yml.
AWS_REGION="${AWS_REGION:-$(group_var aws_region)}"
AWS_REGION="${AWS_REGION:-eu-north-1}"
CLUSTER_NAME="${CLUSTER_NAME:-$(group_var eks_cluster_name)}"
CLUSTER_NAME="${CLUSTER_NAME:-eks-pr1}"
K8S_NAMESPACE="${K8S_NAMESPACE:-$(group_var k8s_namespace)}"
K8S_NAMESPACE="${K8S_NAMESPACE:-hospitalsystem}"
INGRESS_NAME="${INGRESS_NAME:-$(group_var ingress_name)}"
INGRESS_NAME="${INGRESS_NAME:-hospital-ingress}"
ALARM_PREFIX="${ALARM_PREFIX:-$(group_var monitoring_alarm_prefix)}"
ALARM_PREFIX="${ALARM_PREFIX:-hospitalsystem}"

APPLY=false

usage() {
  cat <<'USAGE'
Usage: scripts/cleanup-orphans.sh [--apply]

Lists what a terraform destroy leaves behind in this account and region.
A target group is picked only when it has no load balancer, carries the Load
Balancer Controller's tags for this cluster and Ingress, and its VPC no longer
exists. Alarms are picked by name (ALARM_PREFIX followed by a dash). Before a
destroy, use scripts/pre-destroy-cleanup.sh instead.

  --apply   Delete what is found. Without it the script only lists.
  -h        Show this help.

Environment overrides: AWS_REGION, CLUSTER_NAME, K8S_NAMESPACE, INGRESS_NAME,
ALARM_PREFIX.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=true ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

section() {
  printf '\n== %s ==\n' "$1"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

# Runs a read-only AWS call and prints its text output. Call it as
# var="$(aws_read ...)": a failed lookup then stops the script through set -e
# instead of looking like nothing was found. The CLI prints its own error.
aws_read() {
  local output
  if ! output="$(aws --region "$AWS_REGION" "$@")"; then
    printf 'Lookup failed: aws %s %s\n' "$1" "$2" >&2
    exit 1
  fi
  [ "$output" = None ] || printf '%s\n' "$output"
}

require_command aws

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  printf 'No usable AWS credentials. Run: aws login\n' >&2
  exit 1
fi

found=0
deleted=0

printf 'Region: %s\nCluster: %s\nIngress: %s/%s\nAlarm prefix: %s-\n' \
  "$AWS_REGION" "$CLUSTER_NAME" "$K8S_NAMESPACE" "$INGRESS_NAME" "$ALARM_PREFIX"
if [ "$APPLY" = false ]; then
  printf 'Dry run - nothing will be deleted. Re-run with --apply.\n'
fi

section "Orphaned ALB target groups"

detached="$(
  aws_read elbv2 describe-target-groups \
    --query 'TargetGroups[?starts_with(TargetGroupName, `k8s-`) && length(LoadBalancerArns) == `0`].[TargetGroupArn, VpcId]' \
    --output text
)"

arns=()
vpcs=()
while read -r arn vpc; do
  if [ -n "$arn" ]; then
    arns+=("$arn")
    vpcs+=("$vpc")
  fi
done <<<"$detached"

# The controller tags every target group it creates with its cluster and
# Ingress. Groups without both tags belong to another cluster or Ingress.
tag_query="TagDescriptions[?Tags[?Key=='elbv2.k8s.aws/cluster' && Value=='$CLUSTER_NAME'] && Tags[?Key=='ingress.k8s.aws/stack' && Value=='$K8S_NAMESPACE/$INGRESS_NAME']].ResourceArn"
owned=""
live_vpcs=""
if [ "${#arns[@]}" -gt 0 ]; then
  # describe-tags takes at most 20 ARNs per call.
  for ((i = 0; i < ${#arns[@]}; i += 20)); do
    batch="$(aws_read elbv2 describe-tags --resource-arns "${arns[@]:i:20}" --query "$tag_query" --output text | tr -s '[:space:]' '\n')"
    owned+="$batch"$'\n'
  done
  live_vpcs="$(aws_read ec2 describe-vpcs --query 'Vpcs[].VpcId' --output text | tr -s '[:space:]' '\n')"
fi

orphans=0
others=0
for i in "${!arns[@]}"; do
  arn="${arns[i]}"
  vpc="${vpcs[i]}"
  if ! grep -qxF -- "$arn" <<<"$owned"; then
    others=$((others + 1))
  elif grep -qxF -- "$vpc" <<<"$live_vpcs"; then
    # The controller can detach a group for a moment while it changes the ALB.
    printf 'kept %s: its VPC %s still exists\n' "$arn" "$vpc"
  else
    orphans=$((orphans + 1))
    found=$((found + 1))
    printf '%s\n' "$arn"
    if [ "$APPLY" = true ]; then
      aws elbv2 delete-target-group --region "$AWS_REGION" --target-group-arn "$arn"
      deleted=$((deleted + 1))
    fi
  fi
done

if [ "$orphans" -eq 0 ]; then
  printf 'none\n'
fi
if [ "$others" -gt 0 ]; then
  printf 'Left alone: %d detached k8s-* target group(s) of other clusters or Ingresses\n' "$others"
fi

section "CloudWatch alarms (${ALARM_PREFIX}-*)"

# The dash keeps a project whose name only starts with ALARM_PREFIX out.
alarms="$(
  aws_read cloudwatch describe-alarms \
    --alarm-name-prefix "$ALARM_PREFIX-" \
    --query 'MetricAlarms[].AlarmName' \
    --output text
)"

if [ -z "$alarms" ]; then
  printf 'none\n'
else
  for name in $alarms; do
    found=$((found + 1))
    printf '%s\n' "$name"
  done
  if [ "$APPLY" = true ]; then
    # shellcheck disable=SC2086
    aws cloudwatch delete-alarms --region "$AWS_REGION" --alarm-names $alarms
    for _ in $alarms; do deleted=$((deleted + 1)); done
  fi
fi

section "KMS keys pending deletion (informational)"

keys="$(aws_read kms list-keys --query 'Keys[].KeyId' --output text)"
pending=0
for key in $keys; do
  metadata="$(
    aws_read kms describe-key --key-id "$key" \
      --query 'KeyMetadata.[KeyState,KeyManager,DeletionDate]' \
      --output text
  )"
  read -r state manager date <<<"$metadata"
  if [ "${manager:-}" = "CUSTOMER" ] && [ "${state:-}" = "PendingDeletion" ]; then
    pending=$((pending + 1))
    printf '%s deletes on %s\n' "$key" "$date"
  fi
done

if [ "$pending" -eq 0 ]; then
  printf 'none\n'
else
  printf '\nAWS will not delete these sooner. terraform/kms.tf sets\n'
  printf 'deletion_window_in_days = 30; 7 is the minimum if you rebuild often.\n'
fi

section "Summary"
if [ "$APPLY" = true ]; then
  printf 'Deleted %d of %d orphaned resources.\n' "$deleted" "$found"
else
  printf 'Found %d orphaned resources. Re-run with --apply to delete them.\n' "$found"
fi
