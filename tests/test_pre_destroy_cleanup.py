"""Run scripts/pre-destroy-cleanup.sh against a fake AWS account.

Reuses the fake aws command from test_cleanup_orphans. The default account has
no ALB, target groups, network leftovers or certificate, so only the alarm
section finds anything. The failure tests add one leftover and make AWS refuse
to delete it, and the certificate tests add the certificate.
"""

from pathlib import Path
from unittest import TestCase, main, skipUnless
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

from test_cleanup_orphans import FAKE_AWS


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/pre-destroy-cleanup.sh"
ACCOUNT = {
    "TargetGroups": [],
    "Vpcs": [],
    "MetricAlarms": [
        {"AlarmName": "hospitalsystem-alb-5xx"},
        {"AlarmName": "hospitalsystem-unhealthy-targets-k8s-hospital-backend"},
        {"AlarmName": "hospitalsystem-unhealthy-targets-k8s-hospital-frontend"},
        {"AlarmName": "hospitalsystem-nodegroup-missing-nodes"},
        {"AlarmName": "hospitalsystem2-alb-5xx"},
    ],
    "Keys": [],
}
ALB_ALARMS = [
    "hospitalsystem-alb-5xx",
    "hospitalsystem-unhealthy-targets-k8s-hospital-backend",
    "hospitalsystem-unhealthy-targets-k8s-hospital-frontend",
]


@skipUnless(importlib.util.find_spec("jmespath"), "needs the jmespath package")
class PreDestroyAlarmTests(TestCase):
    def run_cleanup(self, *args, fail="", account=None, errors=None, cert=""):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "aws").write_text(f"#!{sys.executable}\n{FAKE_AWS}")
        # An unreachable cluster, so the script skips the Ingress quickly.
        (tmp / "kubectl").write_text("#!/bin/sh\nexit 1\n")
        for tool in ("aws", "kubectl"):
            (tmp / tool).chmod(0o755)
        self.log = tmp / "deleted.log"
        self.log.touch()
        env = {
            "PATH": f"{tmp}:{os.environ['PATH']}",
            "HOME": str(tmp),
            "AWS_REGION": "eu-north-1",
            "VPC_ID": "vpc-live",
            "ALARM_PREFIX": "hospitalsystem",
            "CERT_ARN": cert,
            "FAKE_ACCOUNT": json.dumps({**ACCOUNT, **(account or {})}),
            "FAKE_LOG": str(self.log),
            "FAKE_FAIL": fail,
            "FAKE_ERRORS": json.dumps(errors or {}),
            "RETRY_DELAY": "0",
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60
        )

    def alarm_section(self, stdout):
        return stdout.split("== ALB alarms (not blocking) ==\n", 1)[1].split("\n==", 1)[0].split()

    def test_dry_run_lists_only_the_alb_alarms(self):
        result = self.run_cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.alarm_section(result.stdout), ALB_ALARMS)
        self.assertEqual(self.log.read_text(), "")

    def test_apply_deletes_only_the_alb_alarms(self):
        result = self.run_cleanup("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), ["delete-alarms " + " ".join(ALB_ALARMS)])


TARGET_GROUP = "arn:aws:elasticloadbalancing:eu-north-1:123456789012:targetgroup/k8s-hospital-backend/1"
CERT = "arn:aws:acm:eu-north-1:123456789012:certificate/00000000-0000-0000-0000-000000000000"
ALB = "arn:aws:elasticloadbalancing:eu-north-1:123456789012:loadbalancer/app/hospital-system-alb/1"


@skipUnless(importlib.util.find_spec("jmespath"), "needs the jmespath package")
class PreDestroyFailureTests(TestCase):
    run_cleanup = PreDestroyAlarmTests.run_cleanup

    def deleted(self):
        return [line for line in self.log.read_text().splitlines() if not line.startswith("delete-alarms")]

    def test_target_group_still_in_use_is_retried(self):
        result = self.run_cleanup(
            "--apply",
            account={"TargetGroups": [{
                "TargetGroupName": "k8s-hospital-backend", "TargetGroupArn": TARGET_GROUP,
                "VpcId": "vpc-live", "LoadBalancerArns": [],
            }]},
            errors={"delete-target-group": ["ResourceInUse", 2]},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), [f"delete-target-group {TARGET_GROUP}"])
        self.assertIn("Now run: ./scripts/tf.sh destroy", result.stdout)

    def test_security_group_that_stays_in_use_fails_the_run(self):
        result = self.run_cleanup(
            "--apply",
            account={"SecurityGroups": [{"GroupId": "sg-k8s"}]},
            errors={"delete-security-group": ["DependencyViolation", -1]},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not delete sg-k8s: An error occurred (DependencyViolation)", result.stderr)
        self.assertIn("1 could not be deleted", result.stderr)
        self.assertNotIn("Now run: ./scripts/tf.sh destroy", result.stdout)
        self.assertEqual(self.deleted(), [])

    def test_already_deleted_security_group_counts_as_deleted(self):
        result = self.run_cleanup(
            "--apply",
            account={"SecurityGroups": [{"GroupId": "sg-k8s"}]},
            errors={"delete-security-group": ["InvalidGroup.NotFound", 1]},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deleted sg-k8s", result.stdout)

    def test_failed_lookup_stops_instead_of_reading_as_none(self):
        for operation in ("describe-load-balancers", "describe-target-groups",
                          "describe-security-groups", "describe-network-interfaces",
                          "describe-alarms", "describe-certificate"):
            with self.subTest(operation=operation):
                result = self.run_cleanup("--apply", fail=operation, cert=CERT)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{operation} failed: An error occurred (AccessDenied)", result.stderr)
                self.assertNotIn("Now run: ./scripts/tf.sh destroy", result.stdout)
                self.assertNotIn("not in use", result.stdout)


@skipUnless(importlib.util.find_spec("jmespath"), "needs the jmespath package")
class PreDestroyCertificateTests(TestCase):
    run_cleanup = PreDestroyAlarmTests.run_cleanup

    def certificate_section(self, certificates):
        result = self.run_cleanup(cert=CERT, account={"Certificates": certificates})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split("== ACM certificate (informational) ==\n", 1)[1].split("\n==", 1)[0]

    def test_unused_certificate_is_left_to_terraform(self):
        section = self.certificate_section([{"CertificateArn": CERT, "InUseBy": []}])
        self.assertIn("not in use - terraform destroy can delete it", section)

    def test_certificate_in_use_names_what_holds_it(self):
        section = self.certificate_section([{"CertificateArn": CERT, "InUseBy": [ALB]}])
        self.assertIn("still in use by:", section)
        self.assertIn(ALB, section)

    def test_deleted_certificate_is_not_called_unused(self):
        section = self.certificate_section([])
        self.assertIn("not found", section)
        self.assertNotIn("not in use", section)


if __name__ == "__main__":
    main()
