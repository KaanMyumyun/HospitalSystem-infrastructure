#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GROUP_VARS_DIR="$REPO_ROOT/ansible/group_vars/all"

group_var() {
  sed -nE "s/^\"?$1\"?:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/p" \
    "$GROUP_VARS_DIR/main.yml" "$GROUP_VARS_DIR/terraform.yml" 2>/dev/null | tail -n 1 || true
}

AWS_REGION="${AWS_REGION:-$(group_var aws_region)}"
AWS_REGION="${AWS_REGION:-eu-north-1}"
VPC_ID="${VPC_ID:-$(group_var vpc_id)}"
ALB_NAME="${ALB_NAME:-$(group_var alb_name)}"
ALB_NAME="${ALB_NAME:-hospital-system-alb}"
INGRESS_NAME="${INGRESS_NAME:-$(group_var ingress_name)}"
INGRESS_NAME="${INGRESS_NAME:-hospital-ingress}"
K8S_NAMESPACE="${K8S_NAMESPACE:-$(group_var k8s_namespace)}"
K8S_NAMESPACE="${K8S_NAMESPACE:-hospitalsystem}"
CERT_ARN="${CERT_ARN:-$(group_var acm_certificate_arn)}"
ALARM_PREFIX="${ALARM_PREFIX:-$(group_var monitoring_alarm_prefix)}"
ALARM_PREFIX="${ALARM_PREFIX:-hospitalsystem}"
# Seconds between retries of an ALB wait or a delete that AWS reports as in use.
RETRY_DELAY="${RETRY_DELAY:-10}"
# The playbooks write the tunnel kubeconfig here and leave ~/.kube/config alone.
export KUBECONFIG="$REPO_ROOT/.generated/kubeconfig"

APPLY=false

usage() {
  cat <<'USAGE'
Usage: scripts/pre-destroy-cleanup.sh [--apply]

  --apply   Delete what is found. Without it the script only lists.
  -h        Show this help.

Environment overrides: AWS_REGION, VPC_ID, ALB_NAME, INGRESS_NAME,
K8S_NAMESPACE, CERT_ARN, ALARM_PREFIX, RETRY_DELAY.

Exits non-zero when a lookup fails or something can't be deleted, so a
failed cleanup is never followed by a destroy that stalls.
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

# Prints what an AWS lookup returns ("None" counts as nothing). The error
# code given first means the resource doesn't exist, which prints nothing;
# "-" accepts no error. Any other error is printed and returns 1, and callers
# stop with `|| exit 1`: an API error must never read as "nothing there".
lookup() {
  local absent="$1" out err status=0
  shift
  err="$(mktemp)"
  out="$(aws "$@" 2>"$err")" || status=$?
  if [ "$status" -eq 0 ]; then
    [ "$out" = None ] || printf '%s' "$out"
  elif [ "$absent" != - ] && grep -qF "($absent)" "$err"; then
    status=0
  else
    printf 'aws %s %s failed: %s\n' "$1" "$2" "$(cat "$err")" >&2
  fi
  rm -f "$err"
  return "$status"
}

# Runs an AWS delete, retrying while AWS reports the resource in use (first
# code). One that is already gone (second code) counts as deleted. Prints the
# last error and returns 1 when it gives up.
delete_with_retry() {
  local in_use="$1" gone="$2" err attempt
  shift 2
  err="$(mktemp)"
  for attempt in $(seq 1 12); do
    if aws "$@" >/dev/null 2>"$err" || grep -qF "($gone)" "$err"; then
      rm -f "$err"
      return 0
    fi
    if ! grep -qF "($in_use)" "$err" || [ "$attempt" -eq 12 ]; then
      break
    fi
    sleep "$RETRY_DELAY"
  done
  printf 'could not delete %s: %s\n' "${*: -1}" "$(cat "$err")" >&2
  rm -f "$err"
  return 1
}

require_command aws

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  printf 'No usable AWS credentials. Run: aws login\n' >&2
  exit 1
fi

if [ -z "$VPC_ID" ]; then
  printf 'Could not determine the VPC id from %s.\n' "$GROUP_VARS_DIR" >&2
  printf 'Pass it explicitly: VPC_ID=vpc-xxxx %s --apply\n' "$0" >&2
  exit 1
fi

found=0
deleted=0
failures=0

printf 'Region:    %s\nVPC:       %s\nALB:       %s\nIngress:   %s/%s\n' \
  "$AWS_REGION" "$VPC_ID" "$ALB_NAME" "$K8S_NAMESPACE" "$INGRESS_NAME"
if [ "$APPLY" = false ]; then
  printf '\nDry run - nothing will be deleted. Re-run with --apply.\n'
fi

alb_arn() {
  lookup LoadBalancerNotFound elbv2 describe-load-balancers \
    --region "$AWS_REGION" --names "$ALB_NAME" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text
}

# wait_for_alb_gone <attempts>: returns 1 if the ALB is still there after them.
wait_for_alb_gone() {
  local arn
  for _ in $(seq 1 "$1"); do
    arn="$(alb_arn)" || exit 1
    [ -z "$arn" ] && return 0
    sleep "$RETRY_DELAY"
  done
  return 1
}

section "Kubernetes Ingress"

ingress_deleted=false
if ! command -v kubectl >/dev/null 2>&1; then
  printf 'kubectl not installed - skipping, will delete the ALB directly\n'
elif ! kubectl get --raw /readyz --request-timeout=15s >/dev/null 2>&1; then
  printf 'Cluster unreachable - skipping, will delete the ALB directly\n'
  printf 'To let the controller delete it instead, open the tunnel first with\n'
  printf 'ansible-playbook ansible/playbooks/kubeconfig.yml\n'
  printf 'If a tunnel from before a cluster rebuild is still open, close it with\n'
  printf 'pkill -f AWS-StartPortForwardingSessionToRemoteHost\n'
elif ! kubectl get ingress "$INGRESS_NAME" -n "$K8S_NAMESPACE" \
  --request-timeout=15s >/dev/null 2>&1; then
  printf 'Ingress %s/%s not present\n' "$K8S_NAMESPACE" "$INGRESS_NAME"
else
  found=$((found + 1))
  printf 'ingress %s/%s\n' "$K8S_NAMESPACE" "$INGRESS_NAME"
  if [ "$APPLY" = true ]; then
    kubectl delete ingress "$INGRESS_NAME" -n "$K8S_NAMESPACE" \
      --ignore-not-found=true --wait=false --request-timeout=20s
    deleted=$((deleted + 1))
    ingress_deleted=true

    printf 'Waiting for the controller to delete the ALB...\n'
    wait_for_alb_gone 18 || printf 'The controller did not delete it in time\n'
  fi
fi

section "Load balancer"

arn="$(alb_arn)" || exit 1
if [ -z "$arn" ]; then
  if [ "$ingress_deleted" = true ]; then
    printf 'gone (deleted by the controller)\n'
  else
    printf 'none\n'
  fi
else
  found=$((found + 1))
  printf '%s\n' "$arn"
  if [ "$APPLY" = true ]; then
    printf 'Controller did not remove it - deleting directly\n'
    aws elbv2 delete-load-balancer --region "$AWS_REGION" --load-balancer-arn "$arn"
    printf 'Waiting for the ALB to disappear...\n'
    if wait_for_alb_gone 30; then
      deleted=$((deleted + 1))
    else
      printf 'the ALB is still there\n' >&2
      failures=$((failures + 1))
    fi
  fi
fi

section "Orphaned ALB target groups"

target_groups="$(
  lookup - elbv2 describe-target-groups \
    --region "$AWS_REGION" \
    --query "TargetGroups[?starts_with(TargetGroupName, \`k8s-\`) && VpcId=='$VPC_ID' && length(LoadBalancerArns) == \`0\`].TargetGroupArn" \
    --output text
)" || exit 1

if [ -z "$target_groups" ]; then
  printf 'none\n'
else
  for tg in $target_groups; do
    found=$((found + 1))
    printf '%s\n' "$tg"
    # Right after the ALB goes, its target groups can still read as in use.
    if [ "$APPLY" = true ]; then
      if delete_with_retry ResourceInUse TargetGroupNotFound \
        elbv2 delete-target-group --region "$AWS_REGION" --target-group-arn "$tg"; then
        deleted=$((deleted + 1))
      else
        failures=$((failures + 1))
      fi
    fi
  done
fi

section "Kubernetes-managed security groups"

k8s_sgs="$(
  lookup - ec2 describe-security-groups \
    --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=k8s-*" \
    --query 'SecurityGroups[].GroupId' --output text
)" || exit 1

if [ -z "$k8s_sgs" ]; then
  printf 'none\n'
else
  for sg in $k8s_sgs; do
    found=$((found + 1))
    printf '%s\n' "$sg"
  done

  if [ "$APPLY" = true ]; then
    for sg in $k8s_sgs; do
      referencing_groups="$(lookup - ec2 describe-security-groups --region "$AWS_REGION" \
        --filters "Name=vpc-id,Values=$VPC_ID" "Name=ip-permission.group-id,Values=$sg" \
        --query 'SecurityGroups[].GroupId' --output text)" || exit 1
      for referencing in $referencing_groups; do
        rules="$(lookup - ec2 describe-security-group-rules --region "$AWS_REGION" \
          --filters "Name=group-id,Values=$referencing" \
          --query "SecurityGroupRules[?!IsEgress && ReferencedGroupInfo.GroupId=='$sg'].SecurityGroupRuleId" \
          --output text)" || exit 1
        if [ -n "$rules" ]; then
          # shellcheck disable=SC2086
          aws ec2 revoke-security-group-ingress --region "$AWS_REGION" \
            --group-id "$referencing" --security-group-rule-ids $rules >/dev/null
          printf 'revoked %s in %s\n' "$rules" "$referencing"
        fi
      done
    done

    # ALB network interfaces can hold a group for a while after the ALB goes.
    for sg in $k8s_sgs; do
      if delete_with_retry DependencyViolation InvalidGroup.NotFound \
        ec2 delete-security-group --region "$AWS_REGION" --group-id "$sg"; then
        printf 'deleted %s\n' "$sg"
        deleted=$((deleted + 1))
      else
        failures=$((failures + 1))
      fi
    done
  fi
fi

section "Detached network interfaces"

enis="$(
  lookup - ec2 describe-network-interfaces \
    --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=status,Values=available" \
    --query 'NetworkInterfaces[].NetworkInterfaceId' --output text
)" || exit 1

if [ -z "$enis" ]; then
  printf 'none\n'
else
  for eni in $enis; do
    found=$((found + 1))
    printf '%s\n' "$eni"
    if [ "$APPLY" = true ]; then
      if delete_with_retry InvalidNetworkInterface.InUse InvalidNetworkInterfaceID.NotFound \
        ec2 delete-network-interface --region "$AWS_REGION" --network-interface-id "$eni"; then
        deleted=$((deleted + 1))
      else
        failures=$((failures + 1))
      fi
    fi
  done
fi

section "ALB alarms (not blocking)"

# ansible/playbooks/monitoring.yml creates these; Terraform doesn't know them.
# The node group alarm is Terraform's and goes with the destroy.
if ! alb_alarms="$(
  aws cloudwatch describe-alarms --region "$AWS_REGION" \
    --alarm-name-prefix "$ALARM_PREFIX-" \
    --query "MetricAlarms[?AlarmName == '$ALARM_PREFIX-alb-5xx' || starts_with(AlarmName, '$ALARM_PREFIX-unhealthy-targets-')].AlarmName" \
    --output text 2>&1
)"; then
  printf 'could not list alarms: %s\n' "$alb_alarms" >&2
elif [ -z "$alb_alarms" ]; then
  printf 'none\n'
else
  for name in $alb_alarms; do printf '%s\n' "$name"; done
  if [ "$APPLY" = true ]; then
    # shellcheck disable=SC2086
    aws cloudwatch delete-alarms --region "$AWS_REGION" --alarm-names $alb_alarms
    printf 'deleted\n'
  fi
fi

section "ACM certificate (informational)"

if [ -z "$CERT_ARN" ]; then
  printf 'no certificate arn in group_vars - skipping\n'
else
  in_use="$(
    aws acm describe-certificate --region "$AWS_REGION" --certificate-arn "$CERT_ARN" \
      --query 'Certificate.InUseBy' --output text 2>/dev/null || true
  )"
  if [ -z "$in_use" ] || [ "$in_use" = "None" ]; then
    printf 'not in use - terraform destroy can delete it\n'
  else
    printf 'still in use by:\n%s\n' "$in_use"
    printf '\nTerraform will fail to delete the certificate while this holds it.\n'
  fi
fi

section "Summary"
if [ "$APPLY" = true ]; then
  printf 'Deleted %d of %d blocking resources.\n' "$deleted" "$found"
  if [ "$failures" -gt 0 ]; then
    printf '%d could not be deleted (see above). Fix that and run this again before ./scripts/tf.sh destroy.\n' \
      "$failures" >&2
    exit 1
  fi
  printf 'Now run: ./scripts/tf.sh destroy\n'
else
  printf 'Found %d blocking resources. Re-run with --apply to delete them.\n' "$found"
fi
