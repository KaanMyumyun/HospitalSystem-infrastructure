#!/usr/bin/env python3
"""List what is left in the AWS account that can cost money, in every region.

Read-only: it only runs describe and list calls, which AWS doesn't charge
for, and deletes nothing. Each finding is one of:

  BILLABLE  costs money while it exists
  FREE      exists but costs nothing (a KMS key waiting to be deleted, a
            target group, an idle Lambda function)
  IGNORED   matches config/account-sweep-ignore.txt, which says why

A service the account or region can't use (Free plan subscriptions, regions
without the service) is skipped. Any other error fails that check, because
an unreadable service can't be called empty.

Exits 0 when nothing billable is found and every check could be read, 1
otherwise. --cost adds one Cost Explorer call (billed at $0.01) for this
month's spend without credits; Cost Explorer lags about a day.
"""

import argparse
import datetime
import fnmatch
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

IGNORE_FILE = Path(__file__).resolve().parents[1] / "config/account-sweep-ignore.txt"

# The service or region can't be used, so there is nothing there to find.
UNAVAILABLE = (
    "SubscriptionRequiredException", "OptInRequired", "Could not connect to the endpoint URL",
    "UnrecognizedClientException", "not supported in this region",
)


class SweepError(RuntimeError):
    pass


class Unavailable(SweepError):
    """The service isn't offered there. Counted, so a skip is never silent."""


def aws(region, service, operation, *args, query=None):
    command = ["aws", service, operation, "--region", region, *args,
               *(["--query", query] if query else []), "--output", "json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise SweepError(f"{service} {operation} timed out") from exc
    if result.returncode != 0:
        error = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else f"exit {result.returncode}"
        # EC2 is in every region, so an EC2 endpoint that can't be reached
        # is a network problem, not a missing service.
        if service != "ec2" and any(marker in error for marker in UNAVAILABLE):
            raise Unavailable(error)
        raise SweepError(error)
    return json.loads(result.stdout) if result.stdout.strip() else None


def rows(region, service, operation, query, *args):
    return aws(region, service, operation, *args, query=query) or []


def size(count):
    return f"{count / 1024 / 1024:.1f} MB stored" if count >= 1024 * 1024 else f"{count / 1024:.1f} KB stored"


# Each check returns [kind, name, detail, billable] for what it finds. A
# query selects one row per resource: its name first, then any details.
def simple(kind, service, operation, query, *args, billable=True):
    def check(region):
        found = []
        for row in rows(region, service, operation, query, *args):
            row = row if isinstance(row, list) else [row]
            detail = " ".join(str(part) for part in row[1:] if part not in (None, ""))
            found.append([kind, str(row[0]), detail, billable])
        return found
    check.__name__ = kind
    return check


def vpc_endpoints(region):
    return [["VPC endpoint", endpoint, kind, kind == "Interface"] for endpoint, kind in rows(
        region, "ec2", "describe-vpc-endpoints",
        "VpcEndpoints[?State!='deleted' && State!='Deleted'].[VpcEndpointId, VpcEndpointType]")]


def log_groups(region):
    # Stored log data bills; an empty group doesn't. AWS updates storedBytes
    # every few hours, so a group written to minutes ago can still read 0.
    return [["Log group", name, size(stored) if stored else "0 KB stored as of AWS's last count", bool(stored)]
            for name, stored in rows(region, "logs", "describe-log-groups", "logGroups[].[logGroupName, storedBytes]")]


def kms_keys(region):
    found = []
    for key in rows(region, "kms", "list-keys", "Keys[].KeyId"):
        meta = aws(region, "kms", "describe-key", "--key-id", key,
                   query="KeyMetadata.[KeyManager, KeyState, DeletionDate, Description]")
        if not meta or meta[0] != "CUSTOMER":
            continue
        manager, state, deletion, description = meta
        if state == "PendingDeletion":
            found.append(["KMS key", key, f"pending deletion until {str(deletion)[:10]}", False])
        else:
            found.append(["KMS key", key, f"{state} {description or ''}".strip(), True])
    return found


def ecs_clusters(region):
    arns = rows(region, "ecs", "list-clusters", "clusterArns")
    if not arns:
        return []
    return [["ECS cluster", name, f"{tasks} running task(s)", tasks > 0] for name, tasks in rows(
        region, "ecs", "describe-clusters", "clusters[].[clusterName, runningTasksCount]", "--clusters", *arns)]


REGIONAL = [
    simple("EC2 instance", "ec2", "describe-instances",
           "Reservations[].Instances[?State.Name!='terminated'][].[InstanceId, InstanceType, State.Name]"),
    simple("EBS volume", "ec2", "describe-volumes", "Volumes[].[VolumeId, Size, State]"),
    simple("EBS snapshot", "ec2", "describe-snapshots", "Snapshots[].[SnapshotId, VolumeSize]",
           "--owner-ids", "self"),
    simple("AMI", "ec2", "describe-images", "Images[].[ImageId, Name]", "--owners", "self"),
    simple("Elastic IP", "ec2", "describe-addresses", "Addresses[].[PublicIp, AllocationId]"),
    simple("NAT gateway", "ec2", "describe-nat-gateways",
           "NatGateways[?State!='deleted'].[NatGatewayId, State]"),
    vpc_endpoints,
    simple("VPN connection", "ec2", "describe-vpn-connections",
           "VpnConnections[?State!='deleted'].[VpnConnectionId, State]"),
    simple("Transit gateway", "ec2", "describe-transit-gateways",
           "TransitGateways[?State!='deleted'].[TransitGatewayId, State]"),
    simple("Client VPN endpoint", "ec2", "describe-client-vpn-endpoints",
           "ClientVpnEndpoints[?Status.Code!='deleted'].[ClientVpnEndpointId, Status.Code]"),
    simple("Capacity reservation", "ec2", "describe-capacity-reservations",
           "CapacityReservations[?State=='active'].[CapacityReservationId, InstanceType]"),
    simple("VPC", "ec2", "describe-vpcs", "Vpcs[?IsDefault==`false`].[VpcId, CidrBlock]", billable=False),
    simple("Network interface", "ec2", "describe-network-interfaces",
           "NetworkInterfaces[].[NetworkInterfaceId, Status, Description]", billable=False),
    simple("Load balancer", "elbv2", "describe-load-balancers", "LoadBalancers[].[LoadBalancerName, Type, State.Code]"),
    simple("Classic load balancer", "elb", "describe-load-balancers", "LoadBalancerDescriptions[].[LoadBalancerName]"),
    simple("Target group", "elbv2", "describe-target-groups", "TargetGroups[].[TargetGroupName]", billable=False),
    simple("EKS cluster", "eks", "list-clusters", "clusters[]"),
    ecs_clusters,
    simple("ECR repository", "ecr", "describe-repositories", "repositories[].[repositoryName]"),
    simple("RDS instance", "rds", "describe-db-instances", "DBInstances[].[DBInstanceIdentifier, DBInstanceClass]"),
    simple("RDS cluster", "rds", "describe-db-clusters", "DBClusters[].[DBClusterIdentifier, Engine]"),
    simple("RDS snapshot", "rds", "describe-db-snapshots", "DBSnapshots[].[DBSnapshotIdentifier]",
           "--snapshot-type", "manual"),
    simple("ElastiCache cluster", "elasticache", "describe-cache-clusters", "CacheClusters[].[CacheClusterId, CacheNodeType]"),
    simple("ElastiCache serverless", "elasticache", "describe-serverless-caches", "ServerlessCaches[].[ServerlessCacheName]"),
    simple("DynamoDB table", "dynamodb", "list-tables", "TableNames[]"),
    simple("OpenSearch domain", "opensearch", "list-domain-names", "DomainNames[].[DomainName]"),
    simple("EFS file system", "efs", "describe-file-systems", "FileSystems[].[FileSystemId, SizeInBytes.Value]"),
    simple("Lambda function", "lambda", "list-functions", "Functions[].[FunctionName]", billable=False),
    simple("CloudWatch alarm", "cloudwatch", "describe-alarms", "MetricAlarms[].[AlarmName]", billable=False),
    log_groups,
    kms_keys,
    simple("Secret", "secretsmanager", "list-secrets", "SecretList[].[Name]"),
    simple("Backup vault", "backup", "list-backup-vaults",
           "BackupVaultList[?NumberOfRecoveryPoints > `0`].[BackupVaultName, NumberOfRecoveryPoints]"),
    simple("Private CA", "acm-pca", "list-certificate-authorities",
           "CertificateAuthorities[?Status!='DELETED'].[Arn, Status]"),
    simple("Resolver endpoint", "route53resolver", "list-resolver-endpoints", "ResolverEndpoints[].[Id, Direction]"),
    simple("Directory", "ds", "describe-directories", "DirectoryDescriptions[].[DirectoryId, Type]"),
    simple("WorkSpace", "workspaces", "describe-workspaces", "Workspaces[].[WorkspaceId, State]"),
    simple("Beanstalk environment", "elasticbeanstalk", "describe-environments",
           "Environments[?Status!='Terminated'].[EnvironmentName, Status]"),
    simple("SageMaker endpoint", "sagemaker", "list-endpoints", "Endpoints[].[EndpointName]"),
    simple("SageMaker notebook", "sagemaker", "list-notebook-instances",
           "NotebookInstances[?NotebookInstanceStatus!='Stopped'].[NotebookInstanceName, NotebookInstanceStatus]"),
    simple("Lightsail instance", "lightsail", "get-instances", "instances[].[name, bundleId]"),
    simple("CloudFormation stack", "cloudformation", "list-stacks",
           "StackSummaries[?StackStatus!='DELETE_COMPLETE'].[StackName, StackStatus]", billable=False),
]

# Global services, read once.
GLOBAL = [
    ("us-east-1", simple("S3 bucket", "s3api", "list-buckets", "Buckets[].[Name]")),
    ("us-east-1", simple("CloudFront distribution", "cloudfront", "list-distributions",
                         "DistributionList.Items[].[Id, DomainName]")),
    ("us-east-1", simple("Route 53 hosted zone", "route53", "list-hosted-zones", "HostedZones[].[Name]")),
    ("us-west-2", simple("Global accelerator", "globalaccelerator", "list-accelerators", "Accelerators[].[Name]")),
]


def load_ignores(path):
    """Lines of `<region> | <kind> | <name>  # reason`; region and name take globs."""
    ignores = []
    if not path.exists():
        return ignores
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        entry, _, reason = line.partition("#")
        if not entry.strip():
            continue
        parts = [part.strip() for part in entry.split("|")]
        if len(parts) != 3 or not all(parts):
            raise SweepError(f"{path}:{number}: expected '<region> | <kind> | <name>  # reason'")
        ignores.append((*parts, reason.strip()))
    return ignores


def ignored(ignores, region, kind, name):
    for pattern_region, pattern_kind, pattern_name, reason in ignores:
        if (fnmatch.fnmatch(region, pattern_region) and pattern_kind == kind
                and fnmatch.fnmatch(name, pattern_name)):
            return reason or "listed in the ignore file"
    return None


def cost():
    today = datetime.date.today()
    start = today.replace(day=1)
    end = today + datetime.timedelta(days=1)
    result = aws("us-east-1", "ce", "get-cost-and-usage",
                 "--time-period", f"Start={start},End={end}", "--granularity", "MONTHLY",
                 "--metrics", "UnblendedCost", "--group-by", "Type=DIMENSION,Key=SERVICE",
                 "--filter", json.dumps({"Not": {"Dimensions": {"Key": "RECORD_TYPE",
                                                                "Values": ["Credit", "Refund"]}}}),
                 query="ResultsByTime[0].Groups[].[Keys[0], Metrics.UnblendedCost.Amount]")
    lines = sorted(((float(amount), service) for service, amount in result or []), reverse=True)
    total = sum(amount for amount, _ in lines)
    print(f"\n== Spend from {start} to today, without credits (Cost Explorer lags about a day) ==")
    for amount, service in lines:
        if amount >= 0.005:
            print(f"{service:<52} ${amount:>8.2f}")
    print(f"{'Total':<52} ${total:>8.2f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", action="append", help="only this region (repeatable); default: every enabled one")
    parser.add_argument("--cost", action="store_true", help="also show this month's spend ($0.01 Cost Explorer call)")
    parser.add_argument("--ignore-file", type=Path, default=IGNORE_FILE)
    args = parser.parse_args()

    try:
        ignores = load_ignores(args.ignore_file)
        account = aws("us-east-1", "sts", "get-caller-identity", query="Account")
        regions = args.region or sorted(aws("us-east-1", "ec2", "describe-regions", query="Regions[].RegionName") or [])
    except (SweepError, OSError) as exc:
        print(f"Cannot start the sweep: {exc}", file=sys.stderr)
        return 1
    print(f"== Account {account}: {len(regions)} region(s), {len(REGIONAL)} checks each, plus global services ==")

    jobs = [(region, check) for region in regions for check in REGIONAL]
    if not args.region:
        jobs += GLOBAL
    findings, errors, skipped = [], [], 0

    def run(job):
        region, check = job
        label = "global" if (region, check) in GLOBAL else region
        name = getattr(check, "__name__", "check")
        try:
            return label, check(region), None, False
        except Unavailable:
            return label, [], None, True
        except SweepError as exc:
            return label, [], f"{name}: {exc}", False
        except (OSError, ValueError, TypeError, IndexError) as exc:
            return label, [], f"{name}: unreadable response ({exc})", False

    # Each call starts the AWS CLI, so the sweep is bound by process starts.
    with ThreadPoolExecutor(max_workers=32) as pool:
        for label, found, error, unavailable in pool.map(run, jobs):
            skipped += unavailable
            if error:
                errors.append((label, error))
            for kind, name, detail, billable in found:
                reason = ignored(ignores, label, kind, name)
                status = "IGNORED" if reason else "BILLABLE" if billable else "FREE"
                findings.append((status, label, kind, name, reason or detail))

    order = {"BILLABLE": 0, "FREE": 1, "IGNORED": 2}
    for status, label, kind, name, detail in sorted(findings, key=lambda f: (order[f[0]], f[1], f[2], f[3])):
        print(f"{status:<9} {label:<15} {kind:<24} {name}{'  (' + detail + ')' if detail else ''}")
    for label, error in sorted(errors):
        print(f"{'ERROR':<9} {label:<15} {error}")

    billable = sum(1 for finding in findings if finding[0] == "BILLABLE")
    counts = {status: sum(1 for finding in findings if finding[0] == status) for status in order}
    print(f"\n{counts['BILLABLE']} billable, {counts['FREE']} free, {counts['IGNORED']} ignored, "
          f"{len(errors)} check(s) failed; {len(jobs) - skipped} of {len(jobs)} checks read, "
          f"{skipped} skipped where the service isn't offered")
    if args.cost:
        try:
            cost()
        except SweepError as exc:
            print(f"ERROR     Could not read Cost Explorer: {exc}")
            errors.append(("global", "ce"))
    if billable:
        print("Something billable is still there (see BILLABLE above).")
    elif errors:
        print("Nothing billable found, but some checks failed, so the account can't be called clean.")
    else:
        print("Nothing billable left.")
    return 1 if billable or errors else 0


if __name__ == "__main__":
    sys.exit(main())
