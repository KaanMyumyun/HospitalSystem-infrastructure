"""Exercise monitoring's real section dispatch and summary using fake CLI tools.

Every external command must match a fixture; the tests never contact a cluster
or AWS. Overrides simulate failed reads independently of the healthy fixtures.
"""

from pathlib import Path
from unittest import TestCase, main
import json
import os
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/python/monitoring.py"
ALB = "arn:aws:elasticloadbalancing:eu-north-1:123:loadbalancer/app/test/1"
TARGET = "arn:aws:elasticloadbalancing:eu-north-1:123:targetgroup/backend/1"
ERROR = {"stderr": "AccessDenied: simulated read failure", "code": 1}

FAKE_TOOL = r'''
import json, os, sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = " ".join(sys.argv[1:])
scenario = json.loads(os.environ["FAKE_SCENARIO"])
with open(os.environ["FAKE_CALLS"], "a") as calls:
    calls.write(tool + " " + args + "\n")
for route in scenario:
    if route["tool"] == tool and all(part in args for part in route["contains"]):
        answer = route["answer"]
        if isinstance(answer, str):
            answer = {"stdout": answer}
        print(answer.get("stdout", ""), end="")
        print(answer.get("stderr", ""), end="", file=sys.stderr)
        sys.exit(answer.get("code", 0))
message = "Unexpected fake command: " + tool + " " + args
with open(os.environ["FAKE_UNEXPECTED"], "a") as unexpected:
    unexpected.write(message + "\n")
print(message, file=sys.stderr)
sys.exit(99)
'''


def route(tool, contains, answer):
    return {"tool": tool, "contains": [contains] if isinstance(contains, str) else contains,
            "answer": answer}


def healthy_routes():
    return [
        route("aws", "sts get-caller-identity", "123\tarn:aws:iam::123:user/test\n"),
        route("aws", "describe-load-balancers", f"{ALB}\talb.example.test\tactive\n"),
        route("aws", "describe-target-groups", TARGET),
        route("aws", "describe-tags", f"{TARGET}\thospitalsystem/hospital-ingress-backend:80"),
        route("aws", "describe-listeners", "443\tHTTPS\tforward\tTLS-policy\n"),
        route("aws", "describe-target-health", "10.0.1.4\t8080\thealthy\tNone\n"),
        route("aws", "cloudwatch describe-alarms", "test-alb-5xx\tOK\t2026-01-01\t1\tHealthy\n"),
        route("aws", "cloudwatch describe-alarm-history", ""),
        route("aws", "ec2 describe-network-interfaces", "10.0.0.5\n"),
        route("ansible-playbook", "ansible/playbooks/kubeconfig.yml", ""),
        route("kubectl", "config current-context", "test-context\n"),
        route("kubectl", "get --raw /readyz", "ok"),
        route("kubectl", "get --raw /api/v1/namespaces/hospitalsystem/services/", "Healthy"),
        route("kubectl", "get --raw /api/v1/namespaces/hospitalsystem/pods/", 'http_requests_received_total{code="200"} 1\n'),
        route("kubectl", ["get nodes", "jsonpath="], "node-1|True|False|False|False|17|false\n"),
        route("kubectl", "get nodes -o wide", "node-1 Ready\n"),
        route("kubectl", "top nodes", "node-1 10m 1% 100Mi 10%\n"),
        route("kubectl", "describe nodes", "Name: node-1\n"),
        route("kubectl", "get pods -A", "node-1\n"),
        route("kubectl", ["get pods", "PHASE:"], "pod-1 Running true 0 <none> <none> <none>\n"),
        route("kubectl", ["get pods", "CPU:"], "pod-1 1 1Gi\n"),
        route("kubectl", ["get pods", "jsonpath={.items[*].metadata.name}"], "pod-1"),
        route("kubectl", ["get pods", "-o wide"], "pod-1 Running\n"),
        route("kubectl", "get deployment/", "1|1"),
        route("kubectl", "get daemonset/", "1|1"),
        route("kubectl", "get apiservice", "True"),
        route("kubectl", "logs -n kube-system", '{"level":"info","msg":"ready"}\n'),
        route("helm", "--failed --pending", ""),
        route("helm", "list -A", "NAME NAMESPACE STATUS\ncontroller kube-system deployed\n"),
        route("kubectl", "get namespace", "namespace/hospitalsystem"),
        route("kubectl", "get deployments", "hospital-backend 1/1\nhospital-frontend 1/1\n"),
        route("kubectl", ["get deployment ", ".spec.replicas}"], "1|1|1|example/app:current"),
        route("kubectl", ["get deployment ", "envFrom"], ""),
        route("kubectl", ["get deployment ", "backend-secrets-hash"], "current-hash"),
        route("kubectl", "rollout status", "deployment successfully rolled out"),
        route("kubectl", "get replicasets", "REVISION DESIRED READY CREATED IMAGE\n"),
        route("kubectl", ["get hpa", "jsonpath="], "app|1|3|True\n"),
        route("kubectl", "get hpa", "app 1/3\n"),
        route("kubectl", "top pods", "pod-1 10m 100Mi\n"),
        route("kubectl", ["get services", "jsonpath="], "hospital-backend hospital-frontend"),
        route("kubectl", "get services", "hospital-backend ClusterIP\n"),
        route("kubectl", "get endpointslices", "true\n"),
        route("kubectl", ["get ingress", "jsonpath="], "alb.example.test"),
        route("kubectl", "get ingress", "hospital-ingress alb.example.test\n"),
        route("kubectl", "get targetgroupbindings", "backend targetgroup\n"),
        route("kubectl", "get externalsecret", "True|synced"),
        route("kubectl", "get secret hospital-backend-secrets", "current-hash"),
        route("kubectl", "get validatingadmissionpolicy", "policy-present"),
        route("kubectl", "get networkpolicy", "10.0.0.5/32\n"),
        route("kubectl", "get events", "No resources found"),
    ]


class MonitoringTests(TestCase):
    def run_check(self, *sections, overrides=(), env_overrides=None):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for tool in ("aws", "kubectl", "ansible-playbook", "helm", "gh", "dig", "curl", "openssl"):
            command = bin_dir / tool
            command.write_text(f"#!{sys.executable}\n{FAKE_TOOL}")
            command.chmod(0o755)
        # Only main.yml, as in CI: a terraform.yml from a live stack would add
        # real values, such as the alert topic, and calls no fixture expects.
        group_vars = tmp / "group_vars"
        group_vars.mkdir()
        (group_vars / "main.yml").write_text((ROOT / "ansible/group_vars/all/main.yml").read_text())
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp), "NO_COLOR": "1",
            "GROUP_VARS_DIR": str(group_vars),
            "AWS_REGION": "eu-north-1", "EKS_CLUSTER_NAME": "test-cluster",
            "NAMESPACE": "hospitalsystem", "BACKEND_DEPLOYMENT": "hospital-backend",
            "FRONTEND_DEPLOYMENT": "hospital-frontend", "INGRESS_NAME": "hospital-ingress",
            "ALB_NAME": "test-alb", "ALARM_PREFIX": "test", "APP_DOMAIN": "app.example.test",
            "FAKE_SCENARIO": json.dumps([*overrides, *healthy_routes()]),
            "FAKE_CALLS": str(tmp / "calls"), "FAKE_UNEXPECTED": str(tmp / "unexpected"),
        }
        env.update(env_overrides or {})
        result = subprocess.run([sys.executable, str(SCRIPT), *sections], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertFalse((tmp / "unexpected").exists(),
                         (tmp / "unexpected").read_text() if (tmp / "unexpected").exists() else "")
        return result

    def assertExit(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertIn("== Summary ==", result.stdout)

    def test_healthy_sections_pass_and_aggregate(self):
        result = self.run_check("system", "workloads", "nodes", "app", "alb", "alarms", "events")
        self.assertExit(result, 0)
        self.assertIn("0 failure(s)", result.stdout)
        self.assertIn("All Helm releases are deployed", result.stdout)
        self.assertIn("hospital-backend: 1/1 ready", result.stdout)

    def test_helm_reads_fail_instead_of_reporting_deployed(self):
        for selector in ("list -A", "--failed --pending"):
            with self.subTest(selector=selector):
                result = self.run_check("system", overrides=[route("helm", selector, ERROR)])
                self.assertExit(result, 1)
                self.assertNotIn("All Helm releases are deployed", result.stdout)
                self.assertIn("simulated read failure", result.stdout)

    def test_failed_helm_release_fails(self):
        result = self.run_check("system", overrides=[route("helm", "--failed --pending", "controller")])
        self.assertExit(result, 1)
        self.assertIn("Helm releases not deployed: controller", result.stdout)

    def test_controller_log_read_failure_is_explicitly_unavailable(self):
        result = self.run_check("system", overrides=[route("kubectl", "logs -n kube-system", ERROR)])
        self.assertExit(result, 0)  # Diagnostic logs are a warning-only check.
        self.assertIn("WARN Could not read the Load Balancer Controller logs", result.stdout)
        self.assertNotIn("No errors in the Load Balancer Controller logs", result.stdout)

    def test_failed_pod_read_fails(self):
        result = self.run_check("system", overrides=[route("kubectl", ["get pods", "PHASE:"], ERROR)])
        self.assertExit(result, 1)
        self.assertIn("Could not list pods in kube-system", result.stdout)
        self.assertNotIn("All 0 pods", result.stdout)

    def test_not_ready_pod_fails(self):
        result = self.run_check("system", overrides=[
            route("kubectl", ["get pods", "PHASE:"], "pod-1 Running false 0 CrashLoopBackOff <none> <none>"),
            route("kubectl", "describe pod", "Events:\n  BackOff\n"),
        ])
        self.assertExit(result, 1)
        self.assertIn("pod-1 is Running and not ready (CrashLoopBackOff)", result.stdout)

    def test_controller_reads_and_malformed_counts_fail(self):
        for response in (ERROR, "", "unknown|unknown", "0|0", "0|1"):
            with self.subTest(response=response):
                result = self.run_check("system", overrides=[route("kubectl", "get deployment/metrics-server", response)])
                self.assertExit(result, 1)
                self.assertNotIn("OK   deployment/metrics-server", result.stdout)

    def test_failed_metrics_api_read_fails(self):
        result = self.run_check("system", overrides=[route("kubectl", "get apiservice", ERROR)])
        self.assertExit(result, 1)
        self.assertNotIn("Metrics API is available", result.stdout)

    def test_failed_tunnel_is_cached_but_other_sections_still_run(self):
        result = self.run_check("system", "app", "alarms", overrides=[route("ansible-playbook", "kubeconfig.yml", ERROR)])
        self.assertExit(result, 1)
        self.assertIn("EKS API not reachable; see the first Kubernetes section", result.stdout)
        self.assertIn("All 1 alarms are OK", result.stdout)

    def test_failed_context_read_does_not_claim_a_valid_context(self):
        result = self.run_check("app", overrides=[route("kubectl", "config current-context", ERROR)])
        self.assertExit(result, 1)
        self.assertNotIn("OK   kubectl context", result.stdout)

    def test_alarm_read_failure_and_alarm_state_fail(self):
        for response in (ERROR, "test-alb-5xx\tALARM\t2026-01-01\t1\tToo many errors"):
            with self.subTest(response=response):
                result = self.run_check("alarms", overrides=[route("aws", "cloudwatch describe-alarms", response)])
                self.assertExit(result, 1)
                self.assertNotIn("All 1 alarms are OK", result.stdout)

    def test_target_health_requires_readable_registered_healthy_targets(self):
        for response in (ERROR, "", "10.0.1.4\t8080\tunhealthy\tTarget.ResponseCodeMismatch"):
            with self.subTest(response=response):
                result = self.run_check("alb", overrides=[route("aws", "describe-target-health", response)])
                self.assertExit(result, 1)
                self.assertNotIn("1/1 targets healthy", result.stdout)

    def test_failed_target_group_read_fails(self):
        result = self.run_check("alb", overrides=[route("aws", "describe-target-groups", ERROR)])
        self.assertExit(result, 1)
        self.assertIn("Could not list the ALB target groups", result.stdout)

    def test_missing_https_listener_fails(self):
        result = self.run_check("alb", overrides=[route("aws", "describe-listeners", "80\tHTTP\tforward\tNone")])
        self.assertExit(result, 1)
        self.assertIn("no HTTPS listener on 443", result.stdout)

    def test_node_reads_and_unready_nodes_fail(self):
        for response in (ERROR, "", "node-1|False|False|False|False|17|false"):
            with self.subTest(response=response):
                result = self.run_check("nodes", overrides=[route("kubectl", ["get nodes", "jsonpath="], response)])
                self.assertExit(result, 1)

    def test_failed_readyz_read_cannot_pass_from_partial_output(self):
        result = self.run_check("nodes", overrides=[route("kubectl", "get --raw /readyz", {"stdout": "ok", "code": 1})])
        self.assertExit(result, 1)
        self.assertNotIn("API server /readyz is ok", result.stdout)

    def test_failed_health_endpoints_fail(self):
        for endpoint in ("hospital-backend:http/proxy/health/ready", "hospital-frontend:http/proxy/health"):
            with self.subTest(endpoint=endpoint):
                result = self.run_check("app", overrides=[route("kubectl", endpoint, ERROR)])
                self.assertExit(result, 1)
                self.assertIn("simulated read failure", result.stdout)

    def test_deployment_reads_and_invalid_replica_counts_fail(self):
        for response in (ERROR, "", "bad|bad|bad|image", "1|0|1|image"):
            with self.subTest(response=response):
                result = self.run_check("workloads", overrides=[route("kubectl", ["get deployment hospital-backend", ".spec.replicas}"], response)])
                self.assertExit(result, 1)
                self.assertNotIn("OK   hospital-backend:", result.stdout)

    def test_failed_rollout_status_is_reported(self):
        result = self.run_check("workloads", overrides=[route("kubectl", "rollout status deployment/hospital-backend", ERROR)])
        self.assertExit(result, 1)
        self.assertIn("Could not read the rollout status of hospital-backend", result.stdout)

    def test_service_endpoint_reads_fail(self):
        result = self.run_check("workloads", overrides=[route("kubectl", "get endpointslices", ERROR)])
        self.assertExit(result, 1)
        self.assertNotIn("has 1 ready endpoint(s)", result.stdout)

    def test_empty_control_plane_lookup_cannot_confirm_network_policy(self):
        result = self.run_check("workloads", overrides=[route("aws", "ec2 describe-network-interfaces", "")])
        self.assertExit(result, 0)
        self.assertIn("WARN No EKS control plane ENIs found", result.stdout)
        self.assertNotIn("NetworkPolicy allows the ALB subnets and the current control plane ENIs", result.stdout)

    def test_unreadable_events_do_not_report_no_warnings(self):
        result = self.run_check("events", overrides=[route("kubectl", "get events", ERROR)])
        self.assertExit(result, 0)
        self.assertIn("Could not read the events", result.stdout)
        self.assertNotIn("No warning events", result.stdout)

    def test_failure_survives_later_healthy_section(self):
        result = self.run_check("alarms", "app", overrides=[route("aws", "cloudwatch describe-alarms", ERROR)])
        self.assertExit(result, 1)
        self.assertIn("Backend /health/ready", result.stdout)
        self.assertIn("1 failure(s)", result.stdout)

    def test_new_aws_sections_report_failed_reads(self):
        cases = (
            ("eks", "eks describe-cluster", "Cluster test-cluster not found"),
            ("metrics", "cloudwatch get-metric-data", "Could not read ALB metrics"),
            ("control-plane", "logs describe-log-groups", "Could not read log group"),
            ("cost", "ce get-cost-and-usage", "Cost Explorer query failed"),
        )
        for section, command, message in cases:
            with self.subTest(section=section):
                result = self.run_check(section, overrides=[route("aws", command, ERROR)])
                self.assertExit(result, 1)
                self.assertIn(message, result.stdout)
                self.assertNotIn("unexpected error", result.stdout)

    def test_eks_section_checks_node_group_and_addons(self):
        result = self.run_check("eks", overrides=[
            route("aws", "eks describe-cluster", "ACTIVE\t1.32\teks.1\t0"),
            route("aws", "eks list-insights", "PASSING\t1.33\tupgrade readiness"),
            route("aws", "eks list-addons", "vpc-cni"),
            route("aws", "eks describe-addon", "ACTIVE\tv1.2\t0"),
            route("aws", "eks list-nodegroups", "workers"),
            route("aws", "eks describe-nodegroup", "ACTIVE\t1\t1\t2\tami-1\t0\tworkers-asg"),
            route("aws", ["autoscaling describe-auto-scaling-groups", "Instances"],
                  "i-123\tt3.medium\teu-north-1a\tInService\tHealthy"),
            route("aws", ["autoscaling describe-auto-scaling-groups", "EnabledMetrics"],
                  "GroupInServiceInstances\tGroupDesiredCapacity"),
        ])
        self.assertExit(result, 0)
        self.assertIn("Add-on vpc-cni is ACTIVE", result.stdout)
        self.assertIn("0 failure(s)", result.stdout)
        self.assertIn("Node group workers is ACTIVE", result.stdout)

    def test_cost_and_control_plane_sections_parse_healthy_data(self):
        cost = self.run_check("cost", overrides=[
            route("aws", "ce get-cost-and-usage", "Amazon EC2\t12.34\nAmazon S3\t0.10\n"),
        ])
        self.assertExit(cost, 0)
        self.assertIn("$    12.44", cost.stdout)
        logs = self.run_check("control-plane", overrides=[
            route("aws", "logs describe-log-groups", "30\t1048576"),
            route("aws", "eks describe-cluster", "api\taudit"),
            route("aws", "logs filter-log-events", ""),
        ])
        self.assertExit(logs, 0)
        self.assertIn("authenticator: nothing matching", logs.stdout)

    def test_logs_read_failure_and_empty_ecr_configuration(self):
        result = self.run_check("logs", overrides=[
            route("kubectl", "logs -n hospitalsystem", ERROR),
            route("kubectl", ["get pods", "restartCount"], ""),
        ])
        self.assertExit(result, 0)
        self.assertIn("Could not read hospital-backend logs", result.stdout)
        self.assertIn("Could not read hospital-frontend logs", result.stdout)

        result = self.run_check("ecr")
        self.assertExit(result, 0)
        self.assertIn("No repository URL for hospital-backend", result.stdout)

    def test_ecr_image_list_failure_is_not_reported_healthy(self):
        result = self.run_check("ecr", overrides=[route("aws", "ecr describe-images", ERROR)],
                                env_overrides={"BACKEND_REPOSITORY_URL": "registry.example/hospital-backend",
                                               "FRONTEND_REPOSITORY_URL": "registry.example/hospital-frontend"})
        self.assertExit(result, 1)
        self.assertIn("Could not list images in hospital-backend", result.stdout)
        self.assertIn("Could not list images in hospital-frontend", result.stdout)

    def test_cicd_reports_failed_ssm_reads_and_skips_unauthenticated_gh(self):
        result = self.run_check("cicd", overrides=[
            route("aws", "ssm describe-document", ERROR),
            route("aws", "ssm list-commands", ERROR),
            route("gh", "auth status", ERROR),
        ])
        self.assertExit(result, 1)
        self.assertIn("Could not read deploy document", result.stdout)
        self.assertIn("Could not list the deploy commands", result.stdout)
        self.assertIn("gh is not logged in", result.stdout)

    def test_endpoints_uses_fake_network_tools(self):
        result = self.run_check("endpoints", overrides=[
            route("dig", "CNAME", "alb.example.test.\n"),
            route("dig", " A", "1.2.3.4\n"),
            route("curl", "http://app.example.test/", "301|0.1|https://app.example.test/|text/html"),
            route("curl", "https://app.example.test/api/health", "404|0.1||application/json"),
            route("curl", "https://app.example.test/", "200|0.1||text/html"),
            route("openssl", "s_client", "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----"),
            route("openssl", "x509", "notAfter=Dec 31 12:00:00 2030 GMT\nissuer=Test CA\n"),
        ])
        self.assertExit(result, 0)
        self.assertIn("HTTP redirects to HTTPS", result.stdout)
        self.assertIn("API requests reach the backend", result.stdout)
        self.assertIn("Served certificate is valid", result.stdout)


if __name__ == "__main__":
    main()
