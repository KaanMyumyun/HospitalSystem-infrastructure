"""Run scripts/cleanup-orphans.sh against a fake AWS account.

The fake aws command evaluates --query with jmespath, the library the AWS CLI
uses, so the script's own selection queries are what gets tested.
"""

from pathlib import Path
from unittest import TestCase, main, skipUnless
import importlib.util
import json
import os
import subprocess
import sys
import tempfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cleanup-orphans.sh"
TG_ARN = "arn:aws:elasticloadbalancing:eu-north-1:123456789012:targetgroup"
ALB_ARN = "arn:aws:elasticloadbalancing:eu-north-1:123456789012:loadbalancer/app/hospital-system-alb/1"
OURS = {
    "elbv2.k8s.aws/cluster": "eks-pr1",
    "ingress.k8s.aws/stack": "hospitalsystem/hospital-ingress",
}
OTHER_CLUSTER = {
    "elbv2.k8s.aws/cluster": "shop-eks",
    "ingress.k8s.aws/stack": "shop/shop-ingress",
}


def target_group(name, vpc, tags, attached=False):
    return {
        "TargetGroupArn": f"{TG_ARN}/{name}/0123456789abcdef",
        "TargetGroupName": name,
        "VpcId": vpc,
        "LoadBalancerArns": [ALB_ARN] if attached else [],
        "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
    }


# More than 20 detached groups come first, so this environment's orphan is only
# found if the script asks describe-tags for every batch.
TARGET_GROUPS = [
    *(target_group(f"k8s-shop-shop-{n:02d}", "vpc-shop-deleted", OTHER_CLUSTER) for n in range(25)),
    target_group("k8s-hospital-hospital-orphan", "vpc-deleted", OURS),
    target_group("k8s-hospital-hospital-live", "vpc-live", OURS),
    target_group("k8s-hospital-hospital-inuse", "vpc-deleted", OURS, attached=True),
    target_group("k8s-hospital-other-ingress", "vpc-deleted",
                 {**OURS, "ingress.k8s.aws/stack": "hospitalsystem/other-ingress"}),
    target_group("k8s-untagged", "vpc-deleted", {}),
    target_group("web-servers", "vpc-deleted", OURS),
]
ORPHAN = f"{TG_ARN}/k8s-hospital-hospital-orphan/0123456789abcdef"
LIVE = f"{TG_ARN}/k8s-hospital-hospital-live/0123456789abcdef"

ACCOUNT = {
    "TargetGroups": TARGET_GROUPS,
    "Vpcs": [{"VpcId": "vpc-live"}, {"VpcId": "vpc-default"}],
    "MetricAlarms": [
        {"AlarmName": "hospitalsystem-alb-5xx"},
        {"AlarmName": "hospitalsystem-unhealthy-targets-k8s-hospital"},
        # Terraform's, and live while the environment is up.
        {"AlarmName": "hospitalsystem-nodegroup-missing-nodes"},
        {"AlarmName": "hospitalsystem-alb-5xx-by-hand"},
        {"AlarmName": "hospitalsystem2-alb-5xx"},
        {"AlarmName": "shop-alb-5xx"},
    ],
    "Keys": [
        {"KeyId": "key-pending", "KeyState": "PendingDeletion", "KeyManager": "CUSTOMER",
         "DeletionDate": "2026-10-01T00:00:00+00:00"},
        {"KeyId": "key-aws", "KeyState": "Enabled", "KeyManager": "AWS", "DeletionDate": None},
    ],
}

FAKE_AWS = r'''
import json, os, sys
from pathlib import Path
import jmespath

account = json.loads(os.environ["FAKE_ACCOUNT"])
MULTI = {"--resource-arns", "--alarm-names", "--filters"}

args, words, options = sys.argv[1:], [], {}
i = 0
while i < len(args):
    if args[i] in MULTI:
        key, i = args[i], i + 1
        options[key] = []
        while i < len(args) and not args[i].startswith("--"):
            options[key].append(args[i])
            i += 1
    elif args[i].startswith("--"):
        options[args[i]] = args[i + 1]
        i += 2
    else:
        words.append(args[i])
        i += 1
service, operation = words


def error(code, message):
    print(f"An error occurred ({code}) when calling the {operation} operation: {message}", file=sys.stderr)
    sys.exit(254)


if operation in os.environ.get("FAKE_FAIL", "").split():
    error("AccessDenied", "fake failure")

# FAKE_ERRORS: {"operation": ["ErrorCode", times]}, where times -1 is always.
planned = json.loads(os.environ.get("FAKE_ERRORS") or "{}")
if operation in planned:
    code, times = planned[operation]
    counter = Path(os.environ["FAKE_LOG"] + "." + operation)
    seen = int(counter.read_text()) if counter.exists() else 0
    if times < 0 or seen < times:
        counter.write_text(str(seen + 1))
        error(code, "fake planned error")

if operation.startswith("delete-"):
    with open(os.environ["FAKE_LOG"], "a") as log:
        values = next((options[key] for key in (
            "--target-group-arn", "--group-id", "--network-interface-id", "--load-balancer-arn",
        ) if key in options), None) or " ".join(options["--alarm-names"])
        log.write(f"{operation} {values}\n")
    sys.exit(0)

groups = account["TargetGroups"]
if operation == "get-caller-identity":
    result = {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test"}
elif operation == "describe-target-groups":
    result = {"TargetGroups": [{k: v for k, v in g.items() if k != "Tags"} for g in groups]}
elif operation == "describe-tags":
    arns = options["--resource-arns"]
    if len(arns) > 20:
        error("ValidationError", "at most 20 resource ARNs")
    result = {"TagDescriptions": [
        {"ResourceArn": g["TargetGroupArn"], "Tags": g["Tags"]}
        for g in groups if g["TargetGroupArn"] in arns
    ]}
elif operation == "describe-vpcs":
    result = {"Vpcs": account["Vpcs"]}
elif operation == "describe-load-balancers":
    found = [lb for lb in account.get("LoadBalancers", []) if lb["LoadBalancerName"] == options.get("--names")]
    if not found:
        error("LoadBalancerNotFound", "One or more load balancers not found")
    result = {"LoadBalancers": found}
elif operation == "describe-security-groups":
    # Lookups of groups that reference another group find none.
    referencing = any(f.startswith("Name=ip-permission.group-id") for f in options.get("--filters", []))
    result = {"SecurityGroups": [] if referencing else account.get("SecurityGroups", [])}
elif operation == "describe-network-interfaces":
    result = {"NetworkInterfaces": account.get("NetworkInterfaces", [])}
elif operation == "describe-alarms":
    prefix = options.get("--alarm-name-prefix", "")
    result = {"MetricAlarms": [a for a in account["MetricAlarms"] if a["AlarmName"].startswith(prefix)]}
elif operation == "list-keys":
    result = {"Keys": [{"KeyId": k["KeyId"]} for k in account["Keys"]]}
elif operation == "describe-key":
    result = {"KeyMetadata": next(k for k in account["Keys"] if k["KeyId"] == options["--key-id"])}
elif operation == "describe-certificate":
    found = [c for c in account.get("Certificates", []) if c["CertificateArn"] == options["--certificate-arn"]]
    if not found:
        error("ResourceNotFoundException", "Could not find certificate")
    result = {"Certificate": found[0]}
else:
    error("Unknown", "fake aws has no " + operation)

if "--query" in options:
    result = jmespath.search(options["--query"], result)


def cell(value):
    return "None" if value is None else str(value)


if options.get("--output") != "text":
    print(json.dumps(result))
elif isinstance(result, list) and result and all(isinstance(row, list) for row in result):
    print("\n".join("\t".join(map(cell, row)) for row in result))
elif isinstance(result, list):
    print("\t".join(map(cell, result)))
else:
    print(cell(result))
'''


@skipUnless(importlib.util.find_spec("jmespath"), "needs the jmespath package")
class CleanupOrphansTests(TestCase):
    def run_cleanup(self, *args, fail=""):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "aws").write_text(f"#!{sys.executable}\n{FAKE_AWS}")
        (tmp / "aws").chmod(0o755)
        self.log = tmp / "deleted.log"
        self.log.touch()
        env = {
            "PATH": f"{tmp}:{os.environ['PATH']}",
            "HOME": str(tmp),
            "AWS_REGION": "eu-north-1",
            "CLUSTER_NAME": "eks-pr1",
            "K8S_NAMESPACE": "hospitalsystem",
            "INGRESS_NAME": "hospital-ingress",
            "ALARM_PREFIX": "hospitalsystem",
            "FAKE_ACCOUNT": json.dumps(ACCOUNT),
            "FAKE_LOG": str(self.log),
            "FAKE_FAIL": fail,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60
        )

    def deleted(self):
        return self.log.read_text().splitlines()

    def test_dry_run_lists_only_this_environments_orphans(self):
        result = self.run_cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = [line for line in result.stdout.splitlines() if line.startswith(TG_ARN)]
        self.assertEqual(listed, [ORPHAN])
        self.assertIn(f"kept {LIVE}: its VPC vpc-live still exists", result.stdout)
        self.assertIn("Left alone: 27 detached k8s-* target group(s)", result.stdout)
        self.assertNotIn("hospitalsystem2-alb-5xx", result.stdout)
        self.assertNotIn("hospitalsystem-nodegroup-missing-nodes", result.stdout)
        self.assertNotIn("hospitalsystem-alb-5xx-by-hand", result.stdout)
        self.assertIn("key-pending deletes on", result.stdout)
        self.assertEqual(self.deleted(), [])

    def test_apply_deletes_only_this_environments_orphans(self):
        result = self.run_cleanup("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), [
            f"delete-target-group {ORPHAN}",
            "delete-alarms hospitalsystem-alb-5xx hospitalsystem-unhealthy-targets-k8s-hospital",
        ])
        self.assertIn("Deleted 3 of 3 orphaned resources.", result.stdout)

    def test_failed_target_group_lookups_stop_before_deleting(self):
        for operation in ("describe-target-groups", "describe-tags", "describe-vpcs"):
            with self.subTest(operation=operation):
                result = self.run_cleanup("--apply", fail=operation)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Lookup failed: aws {'ec2' if operation == 'describe-vpcs' else 'elbv2'} {operation}", result.stderr)
                self.assertNotIn("none", result.stdout)
                self.assertNotIn("Summary", result.stdout)
                self.assertEqual(self.deleted(), [])

    def test_failed_alarm_and_key_lookups_are_errors(self):
        for operation in ("describe-alarms", "list-keys", "describe-key"):
            with self.subTest(operation=operation):
                result = self.run_cleanup(fail=operation)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(operation, result.stderr)
                self.assertNotIn("Summary", result.stdout)


if __name__ == "__main__":
    main()
