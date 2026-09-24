"""Run scripts/rollout-check.sh against fake kubectl, aws and curl commands."""

from pathlib import Path
from unittest import TestCase, main
import json
import os
import subprocess
import sys
import tempfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/rollout-check.sh"

# Deployment reads: generation|observed|spec|pods|updated|ready|available.
# Each read takes the next entry for that Deployment and repeats the last one.
ROLLING = "2|1|1|2|1|1|1"
DONE = "2|2|1|1|1|1|1"
SCALED_TO_ZERO = "1|1|0||||"
API_DOWN = {"error": "Unable to connect to the server: dial tcp 127.0.0.1:8443: connect: connection refused"}

FAKE_KUBECTL = r'''
import json, os, sys
from pathlib import Path

args = [arg for arg in sys.argv[1:] if not arg.startswith("--request-timeout=")]
joined = " ".join(args)
scenario = json.loads(os.environ["FAKE_SCENARIO"])


def reply(answer):
    if isinstance(answer, dict):
        print(answer["error"], file=sys.stderr)
        sys.exit(1)
    print(answer, end="")
    sys.exit(0)


if args[:2] == ["get", "deployment"] and "{.metadata.generation}" in joined:
    counter = Path(os.environ["FAKE_STATE"]) / args[2]
    calls = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(calls + 1))
    states = scenario[args[2]]
    reply(states[min(calls, len(states) - 1)])
if args[:2] == ["get", "deployment"]:
    reply("RollingUpdate|1|0|0|600|10|45|15|1")
if args[:2] == ["config", "current-context"]:
    reply("fake")
if args[:2] == ["get", "namespace"]:
    reply("enabled")
if args[:2] == ["get", "mutatingwebhookconfigurations"]:
    reply("mutatingwebhookconfiguration.admissionregistration.k8s.io/aws-load-balancer-webhook\n")
if args[:1] == ["get"] and args[1].startswith("mutatingwebhookconfiguration."):
    reply("mpod.elbv2.k8s.aws=Ignore\n")
if args[:2] == ["get", "pods"] and "-A" in args:
    reply(scenario.get("running_pods", "node-1\nnode-1\n"))
if args[:2] == ["get", "pods"]:
    reply("hospital-backend-1 hospital-frontend-1")
if args[:2] == ["get", "pod"] and "readinessGates" in joined:
    reply("target-health.elbv2.k8s.aws/k8s-hospital")
if args[:2] == ["get", "pod"]:
    reply("True")
if args[:2] == ["get", "ingress"]:
    reply("deregistration_delay.timeout_seconds=30")
if args[:2] == ["get", "nodes"]:
    reply("node-1   17\n")
if args[:2] == ["describe", "node"]:
    reply("Allocated resources:\n  Resource  Requests     Limits\n"
          "  cpu       250m (12%)   1 (51%)\n  memory    300Mi (10%)  1Gi (35%)\n")
if args[:2] == ["rollout", "restart"]:
    reply(args[2] + " restarted\n")
print("fake kubectl: unexpected call: " + joined, file=sys.stderr)
sys.exit(2)
'''

FAKE_AWS = r'''#!/usr/bin/env bash
case "$*" in
  *"sts get-caller-identity"*) printf '123456789012\tarn:aws:iam::123456789012:user/test\n' ;;
  *describe-load-balancers*) printf 'arn:aws:elasticloadbalancing:eu-north-1:123456789012:loadbalancer/app/hospital-system-alb/1\n' ;;
  *describe-target-groups*) printf 'arn:aws:elasticloadbalancing:eu-north-1:123456789012:targetgroup/k8s-hospital/1\n' ;;
  *describe-target-group-attributes*) printf '30\n' ;;
  *) printf 'fake aws: unexpected call: %s\n' "$*" >&2; exit 2 ;;
esac
'''


class RolloutCheckTests(TestCase):
    def run_check(self, *args, backend=(DONE,), frontend=(DONE,), running_pods=None):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = tmp / "bin"
        state_dir = tmp / "state"
        bin_dir.mkdir()
        state_dir.mkdir()
        fakes = {
            "kubectl": f"#!{sys.executable}\n{FAKE_KUBECTL}",
            "aws": FAKE_AWS,
            "curl": "#!/bin/sh\nprintf '200|application/json'\n",
            "ansible-playbook": "#!/bin/sh\nexit 0\n",
        }
        for name, source in fakes.items():
            (bin_dir / name).write_text(source)
            (bin_dir / name).chmod(0o755)

        scenario = {"hospital-backend": list(backend), "hospital-frontend": list(frontend)}
        if running_pods is not None:
            scenario["running_pods"] = running_pods
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp),
            "AWS_REGION": "eu-north-1",
            "NAMESPACE": "hospitalsystem",
            "BACKEND_DEPLOYMENT": "hospital-backend",
            "FRONTEND_DEPLOYMENT": "hospital-frontend",
            "INGRESS_NAME": "hospital-ingress",
            "ALB_NAME": "hospital-system-alb",
            "APP_DOMAIN": "app.example.test",
            "PROBE_INTERVAL": "0.01",
            "ROLLOUT_TIMEOUT": "30",
            "NO_COLOR": "1",
            "FAKE_SCENARIO": json.dumps(scenario),
            "FAKE_STATE": str(state_dir),
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60
        )

    def assertExit(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def test_healthy_rollout_passes(self):
        result = self.run_check("--restart", backend=[ROLLING, DONE], frontend=[ROLLING, DONE])
        self.assertExit(result, 0)
        self.assertIn("Both Deployments kept at least one ready pod the whole time", result.stdout)

    def test_unreadable_deployments_fail_instead_of_passing(self):
        # list.txt item 63: every read fails while the app still answers 200.
        for broken in (API_DOWN, ""):
            with self.subTest(broken=broken):
                result = self.run_check("--restart", backend=[broken], frontend=[broken])
                self.assertExit(result, 1)
                self.assertNotIn("kept at least one ready pod", result.stdout)
                self.assertNotIn("0/0 of 0", result.stdout)
                self.assertIn("Stopped watching: the Deployments could not be read in 3 samples", result.stdout)
                self.assertIn("Could not read the Deployments in 3 of 3 sample(s)", result.stdout)

    def test_api_lost_mid_rollout_fails(self):
        result = self.run_check(
            "--restart", backend=[ROLLING, API_DOWN], frontend=[ROLLING, API_DOWN]
        )
        self.assertExit(result, 1)
        self.assertIn("in 3 of 4 sample(s)", result.stdout)
        self.assertIn("first error: hospital-backend: Unable to connect to the server", result.stdout)
        self.assertNotIn("kept at least one ready pod", result.stdout)

    def test_one_missed_sample_leaves_ready_pods_unconfirmed(self):
        result = self.run_check(
            "--restart", backend=[ROLLING, API_DOWN, DONE], frontend=[ROLLING, DONE]
        )
        self.assertExit(result, 1)
        self.assertIn("Could not read the Deployments in 1 of 3 sample(s)", result.stdout)
        self.assertNotIn("Stopped watching", result.stdout)

    def test_scale_to_zero_is_reported_not_treated_as_unreadable(self):
        result = self.run_check("--restart", backend=[ROLLING, DONE], frontend=[SCALED_TO_ZERO])
        self.assertExit(result, 0)
        self.assertIn("Scaled to 0 replicas: hospital-frontend", result.stdout)
        self.assertNotIn("Could not read", result.stdout)
        self.assertNotIn("Both Deployments kept", result.stdout)

    def test_failed_reads_while_waiting_are_not_a_rollout(self):
        result = self.run_check("--watch", backend=[DONE, API_DOWN], frontend=[DONE])
        self.assertExit(result, 1)
        self.assertNotIn("A rollout started", result.stdout)
        self.assertIn("Stopped waiting for a rollout", result.stdout)

    def test_unlisted_pods_are_not_counted_as_free_slots(self):
        result = self.run_check(running_pods=API_DOWN)
        self.assertExit(result, 1)
        self.assertIn("Could not list the running pods", result.stdout)
        self.assertNotIn("a rollout needs 2 (one per Deployment)", result.stdout)


if __name__ == "__main__":
    main()
