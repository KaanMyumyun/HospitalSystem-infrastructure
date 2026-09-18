#!/usr/bin/env bash
set -Eeuo pipefail

# Deletes the AWS resources that `terraform destroy` leaves behind, because
# Terraform never created them and so never tracked them:
#
#   - ALB target groups, made by the AWS Load Balancer Controller from the
#     Ingress. The controller deletes the ALB when the Ingress goes, but leaves
#     its target groups behind once the cluster is gone.
#   - CloudWatch alarms, written by scripts/monitoring.sh with the AWS CLI.
#
# KMS keys are only reported, never touched: `terraform destroy` already
# scheduled them, and AWS enforces a 7-30 day wait before a key can actually be
# deleted. A key in PendingDeletion is a successful destroy, not an orphan.
#
# Dry run by default. Pass --apply to actually delete.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GROUP_VARS_DIR="$REPO_ROOT/ansible/group_vars/all"

# Reads a plain top-level value from the Ansible group_vars. terraform.yml is
# itself a Terraform resource, so after a destroy it is gone and every lookup
# here falls through to the default.
group_var() {
  sed -nE "s/^\"?$1\"?:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/p" \
    "$GROUP_VARS_DIR/main.yml" "$GROUP_VARS_DIR/terraform.yml" 2>/dev/null | tail -n 1 || true
}

AWS_REGION="${AWS_REGION:-$(group_var aws_region)}"
AWS_REGION="${AWS_REGION:-eu-north-1}"
ALARM_PREFIX="${ALARM_PREFIX:-$(group_var monitoring_alarm_prefix)}"
ALARM_PREFIX="${ALARM_PREFIX:-hospitalsystem}"

APPLY=false

usage() {
  cat <<'USAGE'
Usage: scripts/cleanup-orphans.sh [--apply]

  --apply   Delete what is found. Without it the script only lists.
  -h        Show this help.

Environment overrides: AWS_REGION, ALARM_PREFIX.
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

require_command aws

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  printf 'No usable AWS credentials. Run: aws login\n' >&2
  exit 1
fi

found=0
deleted=0

printf 'Region: %s\nAlarm prefix: %s\n' "$AWS_REGION" "$ALARM_PREFIX"
if [ "$APPLY" = false ]; then
  printf 'Dry run - nothing will be deleted. Re-run with --apply.\n'
fi

# --- ALB target groups -------------------------------------------------------
# A target group named k8s-* with no load balancer attached belongs to an
# Ingress whose ALB is already gone.
section "Orphaned ALB target groups"

target_groups="$(
  aws elbv2 describe-target-groups \
    --region "$AWS_REGION" \
    --query 'TargetGroups[?starts_with(TargetGroupName, `k8s-`) && length(LoadBalancerArns) == `0`].TargetGroupArn' \
    --output text 2>/dev/null || true
)"

if [ -z "$target_groups" ] || [ "$target_groups" = "None" ]; then
  printf 'none\n'
else
  for arn in $target_groups; do
    found=$((found + 1))
    printf '%s\n' "$arn"
    if [ "$APPLY" = true ]; then
      aws elbv2 delete-target-group --region "$AWS_REGION" --target-group-arn "$arn"
      deleted=$((deleted + 1))
    fi
  done
fi

# --- CloudWatch alarms -------------------------------------------------------
section "CloudWatch alarms (prefix ${ALARM_PREFIX})"

alarms="$(
  aws cloudwatch describe-alarms \
    --region "$AWS_REGION" \
    --alarm-name-prefix "$ALARM_PREFIX" \
    --query 'MetricAlarms[].AlarmName' \
    --output text 2>/dev/null || true
)"

if [ -z "$alarms" ] || [ "$alarms" = "None" ]; then
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

# --- KMS keys (reported only) ------------------------------------------------
section "KMS keys pending deletion (informational)"

pending=0
for key in $(aws kms list-keys --region "$AWS_REGION" --query 'Keys[].KeyId' --output text 2>/dev/null || true); do
  read -r state manager date <<<"$(
    aws kms describe-key --region "$AWS_REGION" --key-id "$key" \
      --query 'KeyMetadata.[KeyState,KeyManager,DeletionDate]' \
      --output text 2>/dev/null || true
  )"
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

# --- Summary -----------------------------------------------------------------
section "Summary"
if [ "$APPLY" = true ]; then
  printf 'Deleted %d of %d orphaned resources.\n' "$deleted" "$found"
else
  printf 'Found %d orphaned resources. Re-run with --apply to delete them.\n' "$found"
fi
