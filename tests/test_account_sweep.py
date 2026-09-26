"""Run scripts/account-sweep.py against a fake AWS account.

The fake aws command evaluates --query with jmespath, the library the AWS CLI
uses, so the script's own selection queries are what gets tested. A service
with no fixture answers as if it had nothing.
"""

from pathlib import Path
from unittest import TestCase, main, skipUnless
import importlib.util
import json
import os
import subprocess
import sys
import tempfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/account-sweep.py"

FAKE_AWS = r'''
import json, os, sys
import jmespath

args, options, words = sys.argv[1:], {}, []
i = 0
while i < len(args):
    if args[i].startswith("--"):
        key, i = args[i], i + 1
        values = []
        while i < len(args) and not args[i].startswith("--"):
            values.append(args[i])
            i += 1
        options[key] = values
    else:
        words.append(args[i])
        i += 1
service, operation = words
region = options.get("--region", [""])[0]
name = f"{service} {operation}" + (f" {options['--key-id'][0]}" if "--key-id" in options else "")
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(f"{region} {name} {' '.join(options.get('--filter', []))}\n")

errors = json.loads(os.environ.get("FAKE_ERRORS", "{}"))
for key in (f"{region} {name}", f"* {name}"):
    if key in errors:
        print(f"\nAn error occurred ({errors[key]}) when calling the {operation} operation: fake", file=sys.stderr)
        sys.exit(254)

account = json.loads(os.environ["FAKE_ACCOUNT"])
if name == "sts get-caller-identity":
    response = {"Account": "123456789012"}
elif name == "ec2 describe-regions":
    response = {"Regions": [{"RegionName": r} for r in account]}
else:
    response = account.get(region, {}).get(name, {})
query = options.get("--query", [None])[0]
print(json.dumps(jmespath.search(query, response) if query else response))
'''

TWO_REGIONS = {"eu-north-1": {}, "us-east-1": {}}


@skipUnless(importlib.util.find_spec("jmespath"), "needs the jmespath package")
class AccountSweepTests(TestCase):
    def sweep(self, *args, account=None, errors=None, ignores=""):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "aws").write_text(f"#!{sys.executable}\n{FAKE_AWS}")
        (tmp / "aws").chmod(0o755)
        (tmp / "ignore.txt").write_text(ignores)
        self.log = tmp / "calls.log"
        self.log.touch()
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--ignore-file", str(tmp / "ignore.txt"), *args],
            capture_output=True, text=True, timeout=120,
            env={"PATH": f"{tmp}:{os.environ['PATH']}", "HOME": str(tmp), "FAKE_LOG": str(self.log),
                 "FAKE_ACCOUNT": json.dumps(account or TWO_REGIONS), "FAKE_ERRORS": json.dumps(errors or {})},
        )

    def lines(self, result, status):
        return [line for line in result.stdout.splitlines() if line.startswith(status)]

    def assertExit(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def test_empty_account_is_clean(self):
        result = self.sweep()
        self.assertExit(result, 0)
        self.assertIn("2 region(s)", result.stdout)
        self.assertIn("0 billable, 0 free, 0 ignored, 0 check(s) failed", result.stdout)
        self.assertIn("Nothing billable left.", result.stdout)
        regions = {line.split()[0] for line in self.log.read_text().splitlines()}
        self.assertLessEqual({"eu-north-1", "us-east-1", "us-west-2"}, regions)

    def test_what_costs_money_is_billable_and_what_does_not_is_free(self):
        account = {"eu-north-1": {
            "ec2 describe-instances": {"Reservations": [{"Instances": [
                {"InstanceId": "i-run", "InstanceType": "t3.small", "State": {"Name": "running"}},
                {"InstanceId": "i-gone", "InstanceType": "t3.small", "State": {"Name": "terminated"}}]}]},
            "ec2 describe-nat-gateways": {"NatGateways": [
                {"NatGatewayId": "nat-up", "State": "available"}, {"NatGatewayId": "nat-old", "State": "deleted"}]},
            "ec2 describe-vpc-endpoints": {"VpcEndpoints": [
                {"VpcEndpointId": "vpce-if", "VpcEndpointType": "Interface", "State": "available"},
                {"VpcEndpointId": "vpce-gw", "VpcEndpointType": "Gateway", "State": "available"}]},
            "kms list-keys": {"Keys": [{"KeyId": "k-on"}, {"KeyId": "k-pending"}, {"KeyId": "k-aws"}]},
            "kms describe-key k-on": {"KeyMetadata": {"KeyManager": "CUSTOMER", "KeyState": "Enabled"}},
            "kms describe-key k-pending": {"KeyMetadata": {"KeyManager": "CUSTOMER", "KeyState": "PendingDeletion",
                                                           "DeletionDate": "2026-10-26T10:00:00+00:00"}},
            "kms describe-key k-aws": {"KeyMetadata": {"KeyManager": "AWS", "KeyState": "Enabled"}},
            "logs describe-log-groups": {"logGroups": [
                {"logGroupName": "/full", "storedBytes": 5 * 1024 * 1024}, {"logGroupName": "/empty", "storedBytes": 0}]},
            "ecs list-clusters": {"clusterArns": ["arn:busy", "arn:idle"]},
            "ecs describe-clusters": {"clusters": [{"clusterName": "busy", "runningTasksCount": 2},
                                                   {"clusterName": "idle", "runningTasksCount": 0}]},
            "eks list-clusters": {"clusters": ["eks-pr1"]},
            "elbv2 describe-target-groups": {"TargetGroups": [{"TargetGroupName": "k8s-tg"}]},
        }}
        result = self.sweep(account=account)
        self.assertExit(result, 1)
        billable = " ".join(self.lines(result, "BILLABLE"))
        free = " ".join(self.lines(result, "FREE"))
        for name in ("i-run", "nat-up", "vpce-if", "k-on", "/full", "busy", "eks-pr1"):
            self.assertIn(name, billable)
        for name in ("vpce-gw", "k-pending", "/empty", "idle", "k8s-tg"):
            self.assertIn(name, free)
        for name in ("i-gone", "nat-old", "k-aws"):
            self.assertNotIn(name, result.stdout)
        self.assertIn("5.0 MB stored", billable)
        self.assertIn("pending deletion until 2026-10-26", free)
        self.assertIn("Something billable is still there", result.stdout)

    def test_ignore_file_marks_known_leftovers(self):
        account = {"eu-north-1": {"logs describe-log-groups": {"logGroups": [
            {"logGroupName": "/aws/lambda/hello", "storedBytes": 2048}]}}}
        result = self.sweep(account=account, ignores="eu-* | Log group | /aws/lambda/*  # not this stack\n")
        self.assertExit(result, 0)
        self.assertEqual(len(self.lines(result, "IGNORED")), 1)
        self.assertIn("(not this stack)", result.stdout)

    def test_bad_ignore_file_stops_before_sweeping(self):
        result = self.sweep(ignores="eu-north-1 Log group /x\n")
        self.assertExit(result, 1)
        self.assertIn("Cannot start the sweep", result.stderr)

    def test_services_the_account_cannot_use_are_skipped_and_counted(self):
        result = self.sweep(errors={"* opensearch list-domain-names": "SubscriptionRequiredException",
                                    "us-east-1 lightsail get-instances": "OptInRequired"})
        self.assertExit(result, 0)
        self.assertEqual(self.lines(result, "ERROR"), [])
        self.assertIn("3 skipped where the service isn't offered", result.stdout)

    def test_unreachable_ec2_is_an_error_not_a_skip(self):
        # EC2 is in every region, so this is a network problem.
        result = self.sweep(errors={"eu-north-1 ec2 describe-instances": "Could not connect to the endpoint URL"})
        self.assertExit(result, 1)
        self.assertEqual(len(self.lines(result, "ERROR")), 1)
        self.assertIn("0 skipped", result.stdout)

    def test_unreadable_service_means_not_clean(self):
        result = self.sweep(errors={"eu-north-1 rds describe-db-instances": "AccessDenied"})
        self.assertExit(result, 1)
        self.assertEqual(len(self.lines(result, "ERROR")), 1)
        self.assertIn("RDS instance", result.stdout)
        self.assertIn("can't be called clean", result.stdout)

    def test_region_option_limits_the_sweep(self):
        result = self.sweep("--region", "eu-north-1")
        self.assertExit(result, 0)
        calls = self.log.read_text()
        self.assertNotIn("describe-regions", calls)
        self.assertNotIn("s3api", calls)
        self.assertEqual({line.split()[0] for line in calls.splitlines()} - {"us-east-1"}, {"eu-north-1"})

    def test_cost_filters_out_credits(self):
        account = {"us-east-1": {"ce get-cost-and-usage": {"ResultsByTime": [{"Groups": [
            {"Keys": ["Amazon EKS"], "Metrics": {"UnblendedCost": {"Amount": "1.20"}}},
            {"Keys": ["Tax"], "Metrics": {"UnblendedCost": {"Amount": "0.001"}}}]}]}}}
        result = self.sweep("--cost", account={**TWO_REGIONS, **account})
        self.assertExit(result, 0)
        self.assertIn('"RECORD_TYPE"', self.log.read_text())
        self.assertIn("Credit", self.log.read_text())
        self.assertRegex(result.stdout, r"Amazon EKS +\$ +1\.20")
        self.assertNotIn("Tax", result.stdout)


if __name__ == "__main__":
    main()
