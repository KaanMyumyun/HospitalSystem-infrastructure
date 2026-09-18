#!/usr/bin/env bash
set -Eeuo pipefail

# Deletes the AWS resources that block `terraform destroy`, because Terraform
# never created them and so never tracked them:
#
#   - The ALB, made by the AWS Load Balancer Controller from the Ingress. Its
#     ENIs sit in the public subnets and block subnet deletion, and it holds
#     the ACM certificate that Terraform tries to delete early in the destroy.
#   - Its target groups.
#   - The k8s-* security groups the controller creates, which block VPC
#     deletion, plus the rules other groups hold referencing them.
#   - Detached (available) ENIs left in the VPC.
#
# This is the same teardown as ansible/playbooks/cleanup-kubernetes.yml, in
# plain AWS CLI, for when the cluster is unreachable or the destroy already
# failed partway. Run it BEFORE ./scripts/tf.sh destroy.
#
# scripts/cleanup-orphans.sh is the post-destroy counterpart.
#
# Dry run by default. Pass --apply to actually delete.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GROUP_VARS_DIR="$REPO_ROOT/ansible/group_vars/all"

# Reads a plain top-level value from the Ansible group_vars. terraform.yml is
# itself a Terraform resource, so after a partial destroy it may be gone and
# lookups fall through to main.yml or the default.
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

APPLY=false

usage() {
  cat <<'USAGE'
Usage: scripts/pre-destroy-cleanup.sh [--apply]

  --apply   Delete what is found. Without it the script only lists.
  -h        Show this help.

Environment overrides: AWS_REGION, VPC_ID, ALB_NAME, INGRESS_NAME,
K8S_NAMESPACE, CERT_ARN.
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

if [ -z "$VPC_ID" ]; then
  printf 'Could not determine the VPC id from %s.\n' "$GROUP_VARS_DIR" >&2
  printf 'Pass it explicitly: VPC_ID=vpc-xxxx %s --apply\n' "$0" >&2
  exit 1
fi

found=0
deleted=0

printf 'Region:    %s\nVPC:       %s\nALB:       %s\nIngress:   %s/%s\n' \
  "$AWS_REGION" "$VPC_ID" "$ALB_NAME" "$K8S_NAMESPACE" "$INGRESS_NAME"
if [ "$APPLY" = false ]; then
  printf '\nDry run - nothing will be deleted. Re-run with --apply.\n'
fi

alb_arn() {
  aws elbv2 describe-load-balancers \
    --region "$AWS_REGION" --names "$ALB_NAME" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true
}

# --- Ingress -----------------------------------------------------------------
# The clean path. The controller deletes the ALB, its security groups, and the
# rule it added to the EKS cluster security group. Deleting the ALB directly
# skips all of that, so try this first and only fall back if it does not work.
section "Kubernetes Ingress"

ingress_deleted=false
if ! command -v kubectl >/dev/null 2>&1; then
  printf 'kubectl not installed - skipping, will delete the ALB directly\n'
elif ! kubectl get --raw /readyz --request-timeout=15s >/dev/null 2>&1; then
  printf 'Cluster unreachable - skipping, will delete the ALB directly\n'
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

    printf 'Waiting up to 180s for the controller to delete the ALB...\n'
    for _ in $(seq 1 18); do
      [ "$(alb_arn)" = "" ] && break
      sleep 10
    done
  fi
fi

# --- Load balancer -----------------------------------------------------------
section "Load balancer"

arn="$(alb_arn)"
if [ -z "$arn" ] || [ "$arn" = "None" ]; then
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
    deleted=$((deleted + 1))

    printf 'Waiting up to 300s for the ALB to disappear...\n'
    for _ in $(seq 1 30); do
      [ "$(alb_arn)" = "" ] && break
      sleep 10
    done
  fi
fi

# --- Target groups -----------------------------------------------------------
# A k8s-* target group with no load balancer attached belongs to an Ingress
# whose ALB is already gone.
section "Orphaned ALB target groups"

target_groups="$(
  aws elbv2 describe-target-groups \
    --region "$AWS_REGION" \
    --query "TargetGroups[?starts_with(TargetGroupName, \`k8s-\`) && VpcId=='$VPC_ID' && length(LoadBalancerArns) == \`0\`].TargetGroupArn" \
    --output text 2>/dev/null || true
)"

if [ -z "$target_groups" ] || [ "$target_groups" = "None" ]; then
  printf 'none\n'
else
  for tg in $target_groups; do
    found=$((found + 1))
    printf '%s\n' "$tg"
    if [ "$APPLY" = true ]; then
      aws elbv2 delete-target-group --region "$AWS_REGION" --target-group-arn "$tg"
      deleted=$((deleted + 1))
    fi
  done
fi

# --- Security groups ---------------------------------------------------------
# Only k8s-* groups: the cluster's own eks-cluster-sg-* goes with the cluster,
# and kubes-vpc-endpoints / hospitalsystem-ops / default belong to Terraform.
section "Kubernetes-managed security groups"

k8s_sgs="$(
  aws ec2 describe-security-groups \
    --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=k8s-*" \
    --query 'SecurityGroups[].GroupId' --output text 2>/dev/null || true
)"

if [ -z "$k8s_sgs" ] || [ "$k8s_sgs" = "None" ]; then
  printf 'none\n'
else
  for sg in $k8s_sgs; do
    found=$((found + 1))
    printf '%s\n' "$sg"
  done

  if [ "$APPLY" = true ]; then
    # A group can't be deleted while another group's rule references it, and
    # the controller adds one to the EKS cluster security group.
    for sg in $k8s_sgs; do
      for referencing in $(aws ec2 describe-security-groups --region "$AWS_REGION" \
        --filters "Name=vpc-id,Values=$VPC_ID" "Name=ip-permission.group-id,Values=$sg" \
        --query 'SecurityGroups[].GroupId' --output text 2>/dev/null || true); do
        rules="$(aws ec2 describe-security-group-rules --region "$AWS_REGION" \
          --filters "Name=group-id,Values=$referencing" \
          --query "SecurityGroupRules[?!IsEgress && ReferencedGroupInfo.GroupId=='$sg'].SecurityGroupRuleId" \
          --output text 2>/dev/null || true)"
        if [ -n "$rules" ] && [ "$rules" != "None" ]; then
          # shellcheck disable=SC2086
          aws ec2 revoke-security-group-ingress --region "$AWS_REGION" \
            --group-id "$referencing" --security-group-rule-ids $rules >/dev/null
          printf 'revoked %s in %s\n' "$rules" "$referencing"
        fi
      done
    done

    # The ALB's network interfaces can take a minute to release after it is gone.
    for sg in $k8s_sgs; do
      for attempt in $(seq 1 12); do
        if aws ec2 delete-security-group --region "$AWS_REGION" --group-id "$sg" 2>/dev/null; then
          printf 'deleted %s\n' "$sg"
          deleted=$((deleted + 1))
          break
        fi
        if [ "$attempt" -eq 12 ]; then
          printf 'could not delete %s - something still uses it\n' "$sg" >&2
        else
          sleep 10
        fi
      done
    done
  fi
fi

# --- Detached network interfaces ---------------------------------------------
# Only 'available' ones. An in-use ENI belongs to something still alive, and
# Terraform removes those with the resource that owns them.
section "Detached network interfaces"

enis="$(
  aws ec2 describe-network-interfaces \
    --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=status,Values=available" \
    --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null || true
)"

if [ -z "$enis" ] || [ "$enis" = "None" ]; then
  printf 'none\n'
else
  for eni in $enis; do
    found=$((found + 1))
    printf '%s\n' "$eni"
    if [ "$APPLY" = true ]; then
      if aws ec2 delete-network-interface --region "$AWS_REGION" \
        --network-interface-id "$eni" 2>/dev/null; then
        deleted=$((deleted + 1))
      else
        printf 'could not delete %s\n' "$eni" >&2
      fi
    fi
  done
fi

# --- ACM certificate (reported only) -----------------------------------------
# Terraform deletes the certificate itself. It only fails if the ALB still
# holds it, so this is the check for whether the destroy will get past it.
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

# --- Summary -----------------------------------------------------------------
section "Summary"
if [ "$APPLY" = true ]; then
  printf 'Deleted %d of %d blocking resources.\n' "$deleted" "$found"
  printf 'Now run: ./scripts/tf.sh destroy\n'
else
  printf 'Found %d blocking resources. Re-run with --apply to delete them.\n' "$found"
fi
