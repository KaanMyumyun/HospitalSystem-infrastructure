"""Run the alarm part of scripts/pre-destroy-cleanup.sh against a fake AWS account.

Reuses the fake aws command from test_cleanup_orphans. The account has no ALB,
target groups or network leftovers, so only the alarm section finds anything.
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
    def run_cleanup(self, *args, fail=""):
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
            "CERT_ARN": "",
            "FAKE_ACCOUNT": json.dumps(ACCOUNT),
            "FAKE_LOG": str(self.log),
            "FAKE_FAIL": fail,
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

    def test_failed_lookup_is_reported_not_taken_for_none(self):
        result = self.run_cleanup("--apply", fail="describe-alarms")
        self.assertIn("could not list alarms: An error occurred (AccessDenied)", result.stderr)
        self.assertEqual(self.alarm_section(result.stdout), [])
        self.assertEqual(self.log.read_text(), "")


if __name__ == "__main__":
    main()
