#!/usr/bin/env python3
"""List what a terraform destroy leaves behind in this account and region.

A target group is picked only when it has no load balancer, carries the Load
Balancer Controller's tags for this cluster and Ingress, and its VPC no longer
exists. Alarms are the ALB alarms monitoring.yml creates, picked by the names
config/alb-alarms.json gives them. Terraform's own alarms are left alone.
Before a destroy, use scripts/python/pre-destroy-cleanup.py instead.

Environment overrides: AWS_REGION, CLUSTER_NAME, K8S_NAMESPACE, INGRESS_NAME,
ALARM_PREFIX.
"""

import argparse

from lib.commands import DROP, SHOW, ScriptError, entrypoint, fields, require_command, run
from lib.config import alb_alarm_query, setting

UNUSED_K8S_GROUPS = ("TargetGroups[?starts_with(TargetGroupName, `k8s-`) && length(LoadBalancerArns) == `0`]"
                     ".[TargetGroupArn, VpcId]")


def section(title):
    print(f"\n== {title} ==")


def aws_read(region, *args):
    """A read-only AWS call's text output.

    A failed lookup stops the script instead of looking like nothing was
    found. The CLI prints its own error.
    """
    result = run(["aws", "--region", region, *args], stderr=SHOW)
    if not result.ok:
        raise ScriptError(f"Lookup failed: aws {args[0]} {args[1]}")
    return "" if result.out == "None" else result.out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="delete what is found; without it the script only lists")
    apply = parser.parse_args().apply

    # terraform.yml is gone after a destroy, so these fall back to the fixed
    # names in terraform/locals.tf, terraform/variables.tf and group_vars/all/main.yml.
    region = setting("AWS_REGION", "aws_region", "eu-north-1")
    cluster = setting("CLUSTER_NAME", "eks_cluster_name", "eks-pr1")
    namespace = setting("K8S_NAMESPACE", "k8s_namespace", "hospitalsystem")
    ingress = setting("INGRESS_NAME", "ingress_name", "hospital-ingress")
    prefix = setting("ALARM_PREFIX", "monitoring_alarm_prefix", "hospitalsystem")
    alarm_query = alb_alarm_query(prefix)

    require_command("aws")
    if not run(["aws", "sts", "get-caller-identity"], stderr=DROP).ok:
        raise ScriptError("No usable AWS credentials. Run: aws login")

    found = deleted = 0
    print(f"Region: {region}\nCluster: {cluster}\nIngress: {namespace}/{ingress}\nAlarm prefix: {prefix}-")
    if not apply:
        print("Dry run - nothing will be deleted. Re-run with --apply.")

    section("Orphaned ALB target groups")
    detached = []
    for line in aws_read(region, "elbv2", "describe-target-groups", "--query", UNUSED_K8S_GROUPS,
                         "--output", "text").splitlines():
        arn, vpc = fields(line, 2, separator=None)
        if arn:
            detached.append((arn, vpc))

    # The controller tags every target group it creates with its cluster and
    # Ingress. Groups without both tags belong to another cluster or Ingress.
    tag_query = (f"TagDescriptions[?Tags[?Key=='elbv2.k8s.aws/cluster' && Value=='{cluster}'] && "
                 f"Tags[?Key=='ingress.k8s.aws/stack' && Value=='{namespace}/{ingress}']].ResourceArn")
    owned = set()
    live_vpcs = set()
    if detached:
        # describe-tags takes at most 20 ARNs per call.
        for start in range(0, len(detached), 20):
            batch = [arn for arn, _ in detached[start:start + 20]]
            owned.update(aws_read(region, "elbv2", "describe-tags", "--resource-arns", *batch,
                                  "--query", tag_query, "--output", "text").split())
        live_vpcs = set(aws_read(region, "ec2", "describe-vpcs", "--query", "Vpcs[].VpcId", "--output", "text").split())

    orphans = others = 0
    for arn, vpc in detached:
        if arn not in owned:
            others += 1
        elif vpc in live_vpcs:
            # The controller can detach a group for a moment while it changes the ALB.
            print(f"kept {arn}: its VPC {vpc} still exists")
        else:
            orphans += 1
            found += 1
            print(arn)
            if apply:
                run(["aws", "elbv2", "delete-target-group", "--region", region, "--target-group-arn", arn],
                    capture=False, check=True)
                deleted += 1
    if not orphans:
        print("none")
    if others:
        print(f"Left alone: {others} detached k8s-* target group(s) of other clusters or Ingresses")

    section("ALB alarms (config/alb-alarms.json)")
    # Exact names, so another project whose name starts with ALARM_PREFIX and
    # Terraform's alarms (still live if the environment is up) are never picked.
    alarms = aws_read(region, "cloudwatch", "describe-alarms", "--alarm-name-prefix", f"{prefix}-",
                      "--query", alarm_query, "--output", "text").split()
    if not alarms:
        print("none")
    else:
        for name in alarms:
            found += 1
            print(name)
        if apply:
            run(["aws", "cloudwatch", "delete-alarms", "--region", region, "--alarm-names", *alarms],
                capture=False, check=True)
            deleted += len(alarms)

    section("KMS keys pending deletion (informational)")
    pending = 0
    for key in aws_read(region, "kms", "list-keys", "--query", "Keys[].KeyId", "--output", "text").split():
        metadata = aws_read(region, "kms", "describe-key", "--key-id", key,
                            "--query", "KeyMetadata.[KeyState,KeyManager,DeletionDate]", "--output", "text")
        state, manager, date = fields(metadata, 3, separator=None)
        if manager == "CUSTOMER" and state == "PendingDeletion":
            pending += 1
            print(f"{key} deletes on {date}")
    if not pending:
        print("none")
    else:
        print("\nAWS will not delete these sooner. terraform/kms.tf sets")
        print("deletion_window_in_days = 30; 7 is the minimum if you rebuild often.")

    section("Summary")
    if apply:
        print(f"Deleted {deleted} of {found} orphaned resources.")
    else:
        print(f"Found {found} orphaned resources. Re-run with --apply to delete them.")
    return 0


if __name__ == "__main__":
    entrypoint(main)
