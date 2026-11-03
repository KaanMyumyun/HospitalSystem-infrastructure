#!/usr/bin/env python3
"""Run every monitoring check for HospitalSystem and end with a summary of
warnings and failures. Exits 1 when any check fails. With no sections it runs
all of them. Nothing in AWS is changed unless --refresh-alarms is given.

./scripts/monitoring.py                    # everything, including cost; changes nothing in AWS
./scripts/monitoring.py --refresh-alarms   # also recreates the ALB alarms (monitoring.yml) first
./scripts/monitoring.py workloads logs     # only the sections you name

Sections:
  endpoints      DNS target, HTTP->HTTPS redirect, HTTPS URLs, TLS and ACM certificate
  alarms         alarm state and recent changes (plus a refresh with --refresh-alarms)
  metrics        ALB requests, 4xx/5xx, latency and unhealthy targets in the lookback window
  alb            load balancer, listeners and health of every target
  eks            control plane, upgrade insights, add-ons, node group, ASG, ops instance
  nodes          node readiness and pressure, usage, pod capacity, requested resources
  system         kube-system pods, controllers, metrics API, Helm, Load Balancer Controller errors
  workloads      deployments, rollouts, pods, HPAs, usage vs limits, services, ingress, config, deploy RBAC
  app            backend readiness (database), frontend health, backend Prometheus metrics
  events         recent Warning events
  logs           app logs, plus the previous container's logs after a restart
  control-plane  errors and denied logins in the EKS control plane logs
  ecr            newest images, scan findings, whether the newest image is deployed,
                 and the commit it was built from vs main (also scripts/image-check.py)
  cicd           SSM deploy history, GitHub Actions runs, repository variables vs Terraform
  cost           month-to-date spend without credits (one Cost Explorer call, $0.01)

Environment overrides: AWS_REGION, EKS_CLUSTER_NAME, NAMESPACE,
BACKEND_DEPLOYMENT, FRONTEND_DEPLOYMENT, INGRESS_NAME, ALB_NAME, ALARM_PREFIX,
ALERT_TOPIC_ARN, APP_DOMAIN, ACM_CERT_ARN, OPS_INSTANCE_ID, DEPLOY_SSM_DOCUMENT,
GITHUB_REPOSITORY, BACKEND_METRICS_PORT (9091), LOG_TAIL_LINES (100),
LOOKBACK_HOURS (3), KUBE_TIMEOUT (30s), NO_COLOR.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import re
import time

from lib.checks import Checks, parse_time, utc_now
from lib.commands import (DROP, SHOW, ScriptError, entrypoint, fields, has_command, last_line, print_tail,
                          require_command, run)
from lib.config import REPO_ROOT, group_vars_dir, setting
from lib.images import check_image

ALL_SECTIONS = ("endpoints", "alarms", "metrics", "alb", "eks", "nodes", "system", "workloads", "app", "events",
                "logs", "control-plane", "ecr", "cicd", "cost")
POD_COLUMNS = ("custom-columns=NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,"
               "RESTARTS:.status.containerStatuses[*].restartCount,"
               "WAITING:.status.containerStatuses[*].state.waiting.reason,"
               "LAST:.status.containerStatuses[*].lastState.terminated.reason,"
               "LAST_AT:.status.containerStatuses[*].lastState.terminated.finishedAt")


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(text):
    """Seconds since 1970 for an ISO 8601 timestamp or openssl's "Mar  4
    12:00:00 2027 GMT"; None for anything else."""
    if not text.strip():
        return None
    moment = parse_time(text)
    if moment is None:
        try:
            moment = datetime.strptime(text.strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return int(moment.timestamp())


def number(value, scale=1):
    """value times scale, rounded to a whole number. Like awk, text that doesn't
    start with a number counts as 0."""
    match = re.match(r"\s*[-+]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?", value)
    return f"{(float(match.group(0)) if match else 0.0) * scale:.0f}"


def as_int(text):
    """text as an integer, or None when it isn't one."""
    return int(text) if re.fullmatch(r"\s*-?[0-9]+\s*", text) else None


def alb_metric(id, label, metric, stat, period, *dimensions):
    """One get-metric-data query for an AWS/ApplicationELB metric."""
    return json.dumps({
        "Id": id, "Label": label,
        "MetricStat": {
            "Metric": {"Namespace": "AWS/ApplicationELB", "MetricName": metric, "Dimensions": [
                {"Name": name, "Value": value} for name, _, value in (d.partition("=") for d in dimensions)
            ]},
            "Period": period, "Stat": stat,
        },
    }, separators=(",", ":"))


class Monitor(Checks):
    def __init__(self, settings, refresh_alarms):
        super().__init__(settings["AWS_REGION"], setting("KUBE_TIMEOUT", default="30s"))
        self.cluster = settings["EKS_CLUSTER_NAME"]
        self.namespace = settings["NAMESPACE"]
        self.backend = settings["BACKEND_DEPLOYMENT"]
        self.frontend = settings["FRONTEND_DEPLOYMENT"]
        self.ingress = settings["INGRESS_NAME"]
        self.alb_name = settings["ALB_NAME"]
        self.alarm_prefix = settings["ALARM_PREFIX"]
        self.app_domain = settings["APP_DOMAIN"]
        self.app_url = f"https://{self.app_domain}"
        self.alert_topic_arn = setting("ALERT_TOPIC_ARN", "monitoring_alert_sns_topic_arn")
        self.acm_cert_arn = setting("ACM_CERT_ARN", "acm_certificate_arn")
        self.ops_instance_id = setting("OPS_INSTANCE_ID", "ops_instance_id")
        self.backend_repository_url = setting("BACKEND_REPOSITORY_URL", "backend_repository_url")
        self.frontend_repository_url = setting("FRONTEND_REPOSITORY_URL", "frontend_repository_url")
        self.deploy_group = setting("DEPLOY_GROUP", "github_actions_deploy_group")
        # terraform/ops.tf names the deploy document after var.project_name, which is also the alarm prefix.
        self.deploy_ssm_document = setting("DEPLOY_SSM_DOCUMENT",
                                           default=f"{self.alarm_prefix}-deploy" if self.alarm_prefix else "")
        self.github_repository = setting("GITHUB_REPOSITORY", "github_repository")
        self.github_deploy_environment = setting("GITHUB_DEPLOY_ENVIRONMENT", "github_deploy_environment")
        self.metrics_port = int(settings["BACKEND_METRICS_PORT"])
        self.log_tail_lines = int(settings["LOG_TAIL_LINES"])
        self.lookback_hours = int(settings["LOOKBACK_HOURS"])
        self.refresh_alarms = refresh_alarms
        self.now = int(time.time())
        self.window_start = self.now - self.lookback_hours * 3600
        # load_alb() fills these in once.
        self.alb_looked_up = False
        self.alb_arn = self.alb_dns = self.alb_state = self.alb_problem = self.tg_problem = ""
        self.tg_arns = []
        self.tg_label = {}

    def load_alb(self):
        """Look up the ALB and its target groups, once; True when the ALB exists."""
        if self.alb_looked_up:
            return bool(self.alb_arn)
        self.alb_looked_up = True
        row = self.aws("elbv2", "describe-load-balancers", "--names", self.alb_name,
                       "--query", "LoadBalancers[0].[LoadBalancerArn, DNSName, State.Code]", "--output", "text")
        if row.ok:
            self.alb_arn, self.alb_dns, self.alb_state = fields(row.out, 3, separator=None)
        elif "LoadBalancerNotFound" in row.out:
            self.alb_problem = (f"Load balancer {self.alb_name} not found; the Load Balancer Controller creates it "
                                "from the Ingress")
        else:
            self.alb_problem = f"Could not look up load balancer {self.alb_name}: {last_line(row.out)}"
        if not self.alb_arn:
            return False

        groups = self.aws("elbv2", "describe-target-groups", "--load-balancer-arn", self.alb_arn,
                          "--query", "TargetGroups[].TargetGroupArn", "--output", "text")
        if groups.ok:
            self.tg_arns = groups.out.split("\n", 1)[0].split()
        else:
            self.tg_problem = f"Could not list the ALB target groups: {last_line(groups.out)}"
        for arn in self.tg_arns:
            self.tg_label[arn] = arn.rsplit(":targetgroup/", 1)[-1].split("/", 1)[0]
        if self.tg_arns:
            # The controller tags each group with the Service and port it serves.
            tags = self.aws("elbv2", "describe-tags", "--resource-arns", *self.tg_arns, "--query",
                            "TagDescriptions[].[ResourceArn, Tags[?Key=='ingress.k8s.aws/resource'].Value | [0]]",
                            "--output", "text", stderr=DROP)
            for line in tags.out.splitlines():
                arn, resource = fields(line, 2)
                if resource and resource != "None":
                    self.tg_label[arn] = resource.removeprefix(f"{self.namespace}/{self.ingress}-").rsplit(":", 1)[0]
        return True

    def http_probe(self, url):
        """code|seconds|redirect|content type for one request."""
        return run(["curl", "-sS", "-o", "/dev/null", "--max-time", "15",
                    "-w", "%{http_code}|%{time_total}|%{redirect_url}|%{content_type}", url], stderr=DROP).out

    def check_pods(self, namespace):
        """Report pods that aren't running and ready, and recent restarts; False
        when the pods couldn't be listed."""
        rows = self.kube("get", "pods", "-n", namespace, "--no-headers", "-o", POD_COLUMNS)
        if not rows.ok:
            self.fail(f"Could not list pods in {namespace}: {last_line(rows.out)}")
            return False
        if not rows.out or rows.out.startswith("No resources found"):
            self.warn(f"No pods in {namespace}")
            return True

        total = bad = 0
        for line in rows.out.split("\n"):
            name, phase, ready, restarts, waiting, last_reason, last_at = fields(line, 7, separator=None)
            if phase == "Succeeded":
                continue
            total += 1
            if phase != "Running" or not re.fullmatch(r"true(,true)*", ready):
                bad += 1
                if waiting != "<none>":
                    self.fail(f"{namespace}/{name} is {phase} and not ready ({waiting})")
                else:
                    self.fail(f"{namespace}/{name} is {phase} and not ready")
                described = self.kube("describe", "pod", "-n", namespace, name, stderr=DROP).out.splitlines()
                events = next((i for i, row in enumerate(described) if row.startswith("Events:")), len(described))
                print_tail("\n".join(described[events:]), 12)

            count = sum(int(item) for item in restarts.split(",") if re.fullmatch(r"[0-9]+", item))
            if count:
                latest = max([moment for moment in map(epoch, last_at.split(",")) if moment is not None], default=0)
                if latest >= self.window_start:
                    self.warn(f"{namespace}/{name} restarted {count} time(s), last at {iso(latest)} ({last_reason})")
                else:
                    self.note(f"{namespace}/{name} restarted {count} time(s), none in the last {self.lookback_hours}h")
        if not bad:
            self.ok(f"All {total} pods in {namespace} are running and ready")
        return True

    def section_endpoints(self):
        self.section("Public endpoints")
        self.sub("DNS")
        target = ""
        if has_command("dig"):
            target = run(["dig", "+short", self.app_domain, "CNAME"], stderr=SHOW).out.split("\n", 1)[0]
            target = target.removesuffix(".")
            answers = run(["dig", "+short", self.app_domain, "A"], stderr=SHOW).out.splitlines()
            addresses = "".join(f"{address} " for address in answers if re.fullmatch(r"[0-9.]+", address))
        else:
            hosts = run(["getent", "ahostsv4", self.app_domain], stderr=SHOW).out.splitlines()
            addresses = "".join(f"{address} " for address in sorted({line.split()[0] for line in hosts if line.split()}))
        print(f"{'CNAME':<10} {target or 'none'}\n{'Addresses':<10} {addresses or 'none'}")

        if not addresses:
            self.fail(f"{self.app_domain} does not resolve")
        if not has_command("dig"):
            self.note("dig is not installed; skipping the CNAME check")
        elif not self.load_alb():
            self.warn(f"DNS target not checked: {self.alb_problem}")
        elif not target:
            self.fail(f"{self.app_domain} has no CNAME record; run ansible-playbook "
                      "ansible/playbooks/cloudflare-dns.yml")
        elif target.lower() == self.alb_dns.lower():
            self.ok(f"{self.app_domain} points at the ALB")
        else:
            self.fail(f"{self.app_domain} points at {target} but the ALB is {self.alb_dns}; run ansible-playbook "
                      "ansible/playbooks/cloudflare-dns.yml")

        self.sub("HTTP")
        if not has_command("curl"):
            self.skip("curl is not installed")
        else:
            url = f"http://{self.app_domain}/"
            code, seconds, redirect, kind = fields(self.http_probe(url), 4, "|")
            print(f"{url:<44} {code}  {seconds}s  -> {redirect or 'none'}")
            if re.fullmatch(r"30[1278]", code) and redirect.startswith("https://"):
                self.ok("HTTP redirects to HTTPS")
            else:
                self.warn(f"{url} returned {code} instead of a redirect to HTTPS")

            for path in ("/", "/health"):
                code, seconds, redirect, kind = fields(self.http_probe(self.app_url + path), 4, "|")
                print(f"{self.app_url + path:<44} {code}  {seconds}s")
                if code == "200":
                    self.ok(f"Frontend {path} answered 200 in {seconds}s")
                else:
                    self.fail(f"Frontend {path} returned {code}")

            url = f"{self.app_url}/api/health"
            code, seconds, redirect, kind = fields(self.http_probe(url), 4, "|")
            print(f"{url:<44} {code}  {seconds}s  {kind}")
            status = as_int(code)
            if code != "000" and status is not None and status < 500 and "json" in kind:
                self.ok(f"API requests reach the backend (HTTP {code} from the app; /api/health is not a backend route)")
            elif code == "000" or (status is not None and status >= 500):
                self.fail(f"{url} returned {code}; the backend isn't answering through the ALB")
            else:
                self.warn(f"{url} returned {code} ({kind}), not a JSON answer from the backend")

        self.sub("TLS certificate")
        if not has_command("openssl"):
            self.skip("openssl is not installed")
        else:
            served = run(["openssl", "s_client", "-connect", f"{self.app_domain}:443", "-servername", self.app_domain],
                         input="", stderr=DROP, timeout=20)
            certificate = run(["openssl", "x509", "-noout", "-enddate", "-issuer"], input=served.out + "\n",
                              stderr=DROP).out.splitlines()
            not_after = "\n".join(line[len("notAfter="):] for line in certificate if line.startswith("notAfter="))
            issuer = "\n".join(line[len("issuer="):] for line in certificate if line.startswith("issuer="))
            expires = epoch(not_after)
            if expires is None:
                self.fail(f"Could not read the TLS certificate served on {self.app_domain}:443")
            else:
                days = int((expires - self.now) / 86400)
                print(f"{'Expires':<10} {not_after}\n{'Issuer':<10} {issuer}")
                if days < 7:
                    self.fail(f"Served certificate expires in {days} days")
                elif days < 30:
                    self.warn(f"Served certificate expires in {days} days; ACM normally renews 60 days ahead")
                else:
                    self.ok(f"Served certificate is valid for {days} more days")

        if self.acm_cert_arn:
            acm = self.aws("acm", "describe-certificate", "--certificate-arn", self.acm_cert_arn, "--query",
                           "Certificate.[Status, NotAfter, RenewalEligibility, length(InUseBy)]", "--output", "text")
            if not acm.ok:
                self.fail(f"Could not describe the ACM certificate: {last_line(acm.out)}")
                return
            status, acm_after, renewal, in_use = fields(acm.out, 4, separator=None)
            print(f"ACM: {status}, expires {acm_after}, renewal {renewal}, attached to {in_use} resource(s)")
            if status == "ISSUED":
                self.ok("ACM certificate is ISSUED")
            else:
                self.fail(f"ACM certificate status is {status}")
            if in_use == "0":
                self.warn("ACM certificate isn't attached to any load balancer")
            elif renewal != "ELIGIBLE":
                self.warn(f"ACM certificate renewal eligibility is {renewal}")

    def section_alarms(self):
        self.section("CloudWatch alarms")
        if not self.refresh_alarms:
            self.note("Alarms not refreshed; run with --refresh-alarms after the target groups change.")
        elif not has_command("ansible-playbook"):
            self.skip("ansible-playbook is not installed; ALB alarms not refreshed")
        else:
            self.sub("Refresh ALB alarms (ansible/playbooks/monitoring.yml)")
            refresh = self.ansible("ansible/playbooks/monitoring.yml")
            if refresh.ok:
                self.ok("ALB 5xx and unhealthy-target alarms match the current target groups")
            else:
                print_tail(refresh.out)
                self.fail("Refreshing the ALB alarms failed")

        self.sub("Current state")
        alarms = self.aws("cloudwatch", "describe-alarms", "--alarm-name-prefix", self.alarm_prefix, "--query",
                          "MetricAlarms[].[AlarmName, StateValue, StateUpdatedTimestamp, length(AlarmActions), "
                          "StateReason]", "--output", "text")
        if not alarms.ok:
            self.fail(f"Could not read CloudWatch alarms: {last_line(alarms.out)}")
            return
        if not alarms.out:
            self.warn(f"No CloudWatch alarms start with {self.alarm_prefix}")
        else:
            total = healthy = silent = 0
            for line in alarms.out.split("\n"):
                name, state, since, actions, reason = fields(line, 5)
                total += 1
                print(f"{state:<18} {since.split('.', 1)[0]:<20} {name}")
                if state == "OK":
                    healthy += 1
                elif state == "ALARM":
                    self.fail(f"{name} is in ALARM: {reason}")
                else:
                    self.warn(f"{name} is {state}: {reason}")
                if actions == "0":
                    silent += 1
            if healthy == total:
                self.ok(f"All {total} alarms are OK")
            if silent:
                self.warn(f"{silent} of {total} alarms have no notification action (re-run ansible-playbook "
                          "ansible/playbooks/monitoring.yml, or ./scripts/tf.py apply for Terraform's)")

        self.sub("Alert emails (SNS)")
        if not self.alert_topic_arn:
            self.skip("monitoring_alert_sns_topic_arn is not set")
        else:
            subscriptions = self.aws("sns", "list-subscriptions-by-topic", "--topic-arn", self.alert_topic_arn,
                                     "--query", "Subscriptions[?Protocol==`email`].[Endpoint, SubscriptionArn]",
                                     "--output", "text")
            if not subscriptions.ok:
                self.fail(f"Could not read the subscriptions of {self.alert_topic_arn}: "
                          f"{last_line(subscriptions.out)}")
            elif not subscriptions.out:
                self.fail(f"No email is subscribed to {self.alert_topic_arn}, so alarms notify no one (set "
                          "ALERT_EMAIL in .env.local and apply)")
            else:
                for line in subscriptions.out.split("\n"):
                    endpoint, subscription = fields(line, 2)
                    if subscription == "PendingConfirmation":
                        self.warn(f"{endpoint} gets no alarm emails until it clicks the link in the "
                                  '"AWS Notification - Subscription Confirmation" email')
                    else:
                        self.ok(f"{endpoint} receives alarm emails")

        self.sub(f"State changes in the last {self.lookback_hours}h")
        history = self.aws("cloudwatch", "describe-alarm-history", "--history-item-type", "StateUpdate",
                           "--start-date", iso(self.window_start), "--query",
                           f"AlarmHistoryItems[?starts_with(AlarmName, '{self.alarm_prefix}')]"
                           ".[Timestamp, AlarmName, HistorySummary]", "--output", "text")
        if not history.ok:
            self.warn(f"Could not read alarm history: {last_line(history.out)}")
            return
        if not history.out:
            self.note("No state changes.")
            return
        for line in sorted(history.out.split("\n"), reverse=True)[:20]:
            when, name, summary = (line.split("\t") + ["", ""])[:3]
            print(f"{when[:19]:<20} {name}: {summary}")

    def section_metrics(self):
        self.section(f"ALB metrics (last {self.lookback_hours}h)")
        if not self.load_alb():
            self.fail(self.alb_problem)
            return
        period = self.lookback_hours * 3600
        end = self.now // 60 * 60
        before = len(self.results)
        lb_dim = f"LoadBalancer={self.alb_arn.split(':loadbalancer/', 1)[-1]}"
        if self.tg_problem:
            self.fail(f"{self.tg_problem}; per-target-group metrics are missing")

        queries = [
            alb_metric("requests", "Requests", "RequestCount", "Sum", period, lb_dim),
            alb_metric("elb5xx", "5xx from the ALB", "HTTPCode_ELB_5XX_Count", "Sum", period, lb_dim),
            alb_metric("elb4xx", "4xx from the ALB", "HTTPCode_ELB_4XX_Count", "Sum", period, lb_dim),
            alb_metric("target5xx", "5xx from targets", "HTTPCode_Target_5XX_Count", "Sum", period, lb_dim),
            alb_metric("target4xx", "4xx from targets", "HTTPCode_Target_4XX_Count", "Sum", period, lb_dim),
            alb_metric("latency", "Response time avg (ms)", "TargetResponseTime", "Average", period, lb_dim),
            alb_metric("latencyp99", "Response time p99 (ms)", "TargetResponseTime", "p99", period, lb_dim),
            alb_metric("connerrors", "Target connection errors", "TargetConnectionErrorCount", "Sum", period, lb_dim),
            alb_metric("rejected", "Rejected connections", "RejectedConnectionCount", "Sum", period, lb_dim),
        ]
        for index, arn in enumerate(self.tg_arns, 1):
            group = arn.rsplit(":", 1)[-1]
            label = self.tg_label.get(arn) or group
            tg_dim = f"TargetGroup={group}"
            queries += [
                alb_metric(f"tgrequests{index}", f"Requests to {label}", "RequestCount", "Sum", period, lb_dim, tg_dim),
                alb_metric(f"tg5xx{index}", f"5xx from {label}", "HTTPCode_Target_5XX_Count", "Sum", period, lb_dim,
                           tg_dim),
                alb_metric(f"tgunhealthy{index}", f"Most unhealthy {label} targets", "UnHealthyHostCount", "Maximum",
                           period, lb_dim, tg_dim),
            ]

        rows = self.aws("cloudwatch", "get-metric-data", "--metric-data-queries", f"[{','.join(queries)}]",
                        "--start-time", iso(end - period), "--end-time", iso(end),
                        "--query", "MetricDataResults[].[Id, Label, sum(Values), max(Values)]", "--output", "text")
        if not rows.ok:
            self.fail(f"Could not read ALB metrics: {last_line(rows.out)}")
            return
        for line in rows.out.split("\n"):
            query, label, total, peak = fields(line, 4)
            if query.startswith("latency"):
                value = "no data" if peak == "None" else number(peak, 1000)
            elif query.startswith("tgunhealthy"):
                value = "no data" if peak == "None" else number(peak)
            else:
                value = number(total)
            print(f"{label:<40} {value}")

            if query in ("elb5xx", "connerrors", "rejected") or query.startswith("tg5xx"):
                if int(value) > 0:
                    self.warn(f"{label}: {value} in the last {self.lookback_hours}h")
            elif query.startswith("tgunhealthy"):
                if value != "no data" and int(value) > 0:
                    self.warn(f"{label}: up to {value} in the last {self.lookback_hours}h")
            elif query == "requests" and int(value) == 0:
                self.note("No requests reached the ALB in this window.")
        if len(self.results) == before:
            self.ok(f"No ALB 5xx, connection errors or unhealthy targets in the last {self.lookback_hours}h")

    def section_alb(self):
        self.section("Load balancer and target health")
        if not self.load_alb():
            self.fail(self.alb_problem)
            return
        print(f"{'Name':<8} {self.alb_name}\n{'State':<8} {self.alb_state}\n{'DNS':<8} {self.alb_dns}")
        if self.alb_state == "active":
            self.ok("ALB is active")
        else:
            self.fail(f"ALB state is {self.alb_state}")

        self.sub("Listeners")
        listeners = self.aws("elbv2", "describe-listeners", "--load-balancer-arn", self.alb_arn, "--query",
                             "Listeners[].[Port, Protocol, DefaultActions[0].Type, SslPolicy]", "--output", "text")
        if listeners.ok:
            rows = [(line.split("\t") + ["", "", ""])[:4] for line in listeners.out.split("\n")]
            for port, protocol, action, policy in rows:
                print(f"{port:<6} {protocol:<6} {action:<16} {'' if policy == 'None' else policy}")
            if not any(port == "443" and protocol == "HTTPS" for port, protocol, _, _ in rows):
                self.fail("The ALB has no HTTPS listener on 443")
        else:
            self.fail(f"Could not list ALB listeners: {last_line(listeners.out)}")

        if not self.tg_arns:
            self.fail(self.tg_problem or "No target groups are attached to the ALB")
            return
        for arn in self.tg_arns:
            label = self.tg_label.get(arn, "")
            self.sub(f"Targets: {label}")
            health = self.aws("elbv2", "describe-target-health", "--target-group-arn", arn, "--query",
                              "TargetHealthDescriptions[].[Target.Id, Target.Port, TargetHealth.State, "
                              "TargetHealth.Reason]", "--output", "text")
            if not health.ok:
                self.fail(f"Could not read target health for {label}: {last_line(health.out)}")
                continue
            total = 0
            problems = []
            for line in health.out.split("\n"):
                target, port, state, reason = fields(line, 4)
                if not target:
                    continue
                total += 1
                print(f"{target:<16} {port:<6} {state:<10} {'' if reason == 'None' else reason}")
                if state != "healthy":
                    problems.append(f"{state}/{reason}")
            if not total:
                self.fail(f"{label} has no registered targets")
            elif problems:
                self.fail(f"{label}: {len(problems)} of {total} targets not healthy ({', '.join(problems)})")
            else:
                self.ok(f"{label}: {total}/{total} targets healthy")

    def section_eks(self):
        self.section("EKS (AWS side)")
        self.sub("Control plane")
        info = self.aws("eks", "describe-cluster", "--name", self.cluster, "--query",
                        "cluster.[status, version, platformVersion, length(health.issues || `[]`)]", "--output", "text")
        if not info.ok:
            self.fail(f"Cluster {self.cluster} not found: {last_line(info.out)}")
            return
        status, version, platform, issues = fields(info.out, 4, separator=None)
        print(f"{'Status':<10} {status}\n{'Version':<10} {version}\n{'Platform':<10} {platform}")
        if status == "ACTIVE":
            self.ok(f"Cluster {self.cluster} is ACTIVE on Kubernetes {version}")
        else:
            self.fail(f"Cluster {self.cluster} is {status}")
        if issues != "0":
            self.fail(f"Cluster reports {issues} health issue(s)")
            details = self.aws("eks", "describe-cluster", "--name", self.cluster, "--query",
                               "cluster.health.issues[].[code, message]", "--output", "text")
            print_tail(details.out, 30)

        self.sub("Upgrade insights")
        insights = self.aws("eks", "list-insights", "--cluster-name", self.cluster, "--query",
                            "insights[].[insightStatus.status, kubernetesVersion, name]", "--output", "text")
        if not insights.ok:
            self.warn(f"Could not list upgrade insights: {last_line(insights.out)}")
        elif not insights.out:
            self.note("No insights.")
        else:
            for line in insights.out.splitlines():
                state, k8s_version, name = fields(line, 3)
                print(f"{state:<9} {k8s_version:<6} {name}")
                if state != "PASSING":
                    self.warn(f"Upgrade insight '{name}' is {state} for {k8s_version}")

        self.sub("Managed add-ons")
        addons = self.aws("eks", "list-addons", "--cluster-name", self.cluster,
                          "--query", "addons", "--output", "text")
        if not addons.ok:
            self.fail(f"Could not list the EKS add-ons: {last_line(addons.out)}")
        elif not addons.out:
            self.warn("No managed add-ons; terraform/modules/eks manages vpc-cni, coredns and kube-proxy, "
                      "so run ./scripts/tf.py apply")
        else:
            for addon in addons.out.split():
                row = self.aws("eks", "describe-addon", "--cluster-name", self.cluster, "--addon-name", addon,
                               "--query", "addon.[status, addonVersion, length(health.issues || `[]`)]",
                               "--output", "text")
                if not row.ok:
                    self.fail(f"Could not describe add-on {addon}: {last_line(row.out)}")
                    continue
                state, addon_version, count = fields(row.out, 3, separator=None)
                print(f"{addon:<24} {state:<10} {addon_version}")
                if state == "ACTIVE" and count == "0":
                    self.ok(f"Add-on {addon} is ACTIVE")
                else:
                    self.fail(f"Add-on {addon} is {state} with {count} issue(s)")

        groups = self.aws("eks", "list-nodegroups", "--cluster-name", self.cluster,
                          "--query", "nodegroups", "--output", "text")
        if not groups.ok:
            self.fail(f"Could not list the node groups: {last_line(groups.out)}")
        elif not groups.out:
            self.fail(f"Cluster {self.cluster} has no node groups")
        for group in groups.out.split() if groups.ok else ():
            self.sub(f"Node group {group}")
            row = self.aws("eks", "describe-nodegroup", "--cluster-name", self.cluster,
                           "--nodegroup-name", group, "--query",
                           "nodegroup.[status, scalingConfig.minSize, scalingConfig.desiredSize, "
                           "scalingConfig.maxSize, releaseVersion, length(health.issues || `[]`), "
                           "resources.autoScalingGroups[0].name]", "--output", "text")
            if not row.ok:
                self.fail(f"Could not describe node group {group}: {last_line(row.out)}")
                continue
            state, minimum, desired, maximum, release, count, asg = fields(row.out, 7, separator=None)
            print(f"{'Status':<10} {state}\n{'Scaling':<10} min {minimum} / desired {desired} / max {maximum}"
                  f"\n{'AMI':<10} {release}\n{'ASG':<10} {asg}")
            if state == "ACTIVE":
                self.ok(f"Node group {group} is ACTIVE with {desired} desired node(s)")
            else:
                self.fail(f"Node group {group} is {state}")
            if desired == "0":
                self.warn(f"Node group {group} is scaled to 0 nodes")
            if count != "0":
                self.fail(f"Node group {group} reports {count} health issue(s)")
                details = self.aws("eks", "describe-nodegroup", "--cluster-name", self.cluster,
                                   "--nodegroup-name", group, "--query",
                                   "nodegroup.health.issues[].[code, message]", "--output", "text")
                print_tail(details.out, 30)
            if asg and asg != "None":
                instances = self.aws("autoscaling", "describe-auto-scaling-groups", "--auto-scaling-group-names",
                                     asg, "--query", "AutoScalingGroups[0].Instances[].[InstanceId, InstanceType, "
                                     "AvailabilityZone, LifecycleState, HealthStatus]", "--output", "text")
                if not instances.ok:
                    self.fail(f"Could not read the instances in {asg}: {last_line(instances.out)}")
                else:
                    healthy = 0
                    for line in instances.out.splitlines():
                        instance, kind, zone, lifecycle, health = fields(line, 5)
                        print(f"  {instance:<20} {kind:<10} {zone:<12} {lifecycle:<10} {health}")
                        healthy += lifecycle == "InService" and health == "Healthy"
                    if as_int(desired) is not None and healthy < int(desired):
                        self.fail(f"Only {healthy} of {desired} node instances are InService and Healthy")
                metrics = self.aws("autoscaling", "describe-auto-scaling-groups", "--auto-scaling-group-names",
                                   asg, "--query", "AutoScalingGroups[0].EnabledMetrics[].Metric", "--output", "text")
                if not metrics.ok:
                    self.warn(f"Could not read the group metrics on {asg}: {last_line(metrics.out)}")
                elif not {"GroupInServiceInstances", "GroupDesiredCapacity"}.issubset(metrics.out.split()):
                    self.warn(f"Group metrics are off on {asg}, so {self.alarm_prefix}-nodegroup-missing-nodes "
                              "gets no data (fix: aws autoscaling enable-metrics-collection "
                              f"--auto-scaling-group-name {asg} --granularity 1Minute "
                              "--metrics GroupDesiredCapacity GroupInServiceInstances)")

        self.sub("Ops instance (SSM path to the private API)")
        if not self.ops_instance_id:
            self.skip("ops_instance_id is not set")
            return
        ops = self.aws("ec2", "describe-instance-status", "--instance-ids", self.ops_instance_id,
                       "--include-all-instances", "--query",
                       "InstanceStatuses[0].[InstanceState.Name, SystemStatus.Status, InstanceStatus.Status]",
                       "--output", "text")
        if not ops.ok:
            self.fail(f"Ops instance {self.ops_instance_id} not found: {last_line(ops.out)}")
            return
        state, system_status, instance_status = fields(ops.out, 3, separator=None)
        print(f"{'Instance':<10} {self.ops_instance_id}\n{'State':<10} {state}\n{'Checks':<10} "
              f"system {system_status}, instance {instance_status}")
        if state == "running" and system_status == instance_status == "ok":
            self.ok("Ops instance is running and passes its status checks")
        elif state == "running":
            self.warn(f"Ops instance status checks: system {system_status}, instance {instance_status}")
        else:
            self.fail(f"Ops instance is {state}")
        ping = self.aws("ssm", "describe-instance-information", "--filters",
                        f"Key=InstanceIds,Values={self.ops_instance_id}", "--query",
                        "InstanceInformationList[0].[PingStatus, AgentVersion, LastPingDateTime]", "--output", "text")
        if not ping.ok:
            self.fail(f"Could not read the SSM agent status of the ops instance: {last_line(ping.out)}")
            return
        status, agent, last_ping = fields(ping.out, 3, separator=None)
        if status in ("", "None"):
            self.fail("Ops instance is not registered with SSM; kubectl and deploys can't reach the cluster")
        elif status == "Online":
            self.ok(f"SSM agent {agent} is Online")
        else:
            self.fail(f"SSM agent is {status or 'unknown'} (last ping {last_ping or 'never'}); "
                      "kubectl and deploys can't reach the cluster")

    def section_nodes(self):
        self.section("Kubernetes nodes")
        if not self.ensure_kube():
            return
        readyz = self.kube("get", "--raw", "/readyz")
        if not readyz.ok:
            self.fail(f"Could not read API server /readyz: {last_line(readyz.out)}")
        elif readyz.out == "ok":
            self.ok("API server /readyz is ok")
        else:
            self.fail(f"API server /readyz: {last_line(readyz.out)}")

        self.sub("Nodes")
        print(self.kube("get", "nodes", "-o", "wide").out)
        rows = self.kube("get", "nodes", "-o",
                         'jsonpath={range .items[*]}{.metadata.name}{"|"}'
                         '{.status.conditions[?(@.type=="Ready")].status}{"|"}'
                         '{.status.conditions[?(@.type=="MemoryPressure")].status}{"|"}'
                         '{.status.conditions[?(@.type=="DiskPressure")].status}{"|"}'
                         '{.status.conditions[?(@.type=="PIDPressure")].status}{"|"}'
                         '{.status.allocatable.pods}{"|"}{.spec.unschedulable}{"\\n"}{end}')
        if not rows.ok:
            self.fail(f"Could not read the node conditions: {last_line(rows.out)}")
            return
        capacity = {}
        for line in rows.out.splitlines():
            name, ready, memory, disk, pid, pods, cordoned = fields(line, 7, "|")
            if not name:
                continue
            capacity[name] = pods
            if ready != "True":
                self.fail(f"Node {name} is not Ready ({ready})")
            if (memory, disk, pid) != ("False", "False", "False"):
                self.warn(f"Node {name} pressure: memory={memory} disk={disk} pid={pid}")
            if cordoned == "true":
                self.warn(f"Node {name} is cordoned")
        if not capacity:
            self.fail("The cluster has no nodes")
            return

        self.sub("Usage")
        usage = self.kube("top", "nodes")
        print(usage.out)
        if not usage.ok:
            self.warn("kubectl top nodes failed; metrics-server may not be ready")

        self.sub("Pod capacity")
        placements = self.kube("get", "pods", "-A", "--field-selector=status.phase!=Succeeded,status.phase!=Failed",
                               "-o", 'jsonpath={range .items[*]}{.spec.nodeName}{"\\n"}{end}')
        if not placements.ok:
            self.warn(f"Could not count the pods on each node: {last_line(placements.out)}")
        else:
            from collections import Counter
            counts = Counter(name for name in placements.out.splitlines() if name)
            for name, pods in capacity.items():
                count = counts[name]
                print(f"{name:<48} {count}/{pods} pods")
                if as_int(pods) and int(pods) > 0 and count * 100 // int(pods) >= 90:
                    self.warn(f"Node {name} runs {count} of its {pods} pod slots; new pods may not schedule")

        self.sub("Requested resources")
        described = self.kube("describe", "nodes")
        show = False
        for line in described.out.splitlines():
            if line.startswith("Name:"):
                print(line)
                continue
            if line.startswith("Allocated resources:"):
                show = True
            if line.startswith("Events:"):
                show = False
            if show:
                print(line)

    def section_system(self):
        self.section("Cluster add-ons (kube-system)")
        if not self.ensure_kube():
            return
        self.sub("Pods")
        print(self.kube("get", "pods", "-n", "kube-system", "-o", "wide").out)
        self.check_pods("kube-system")

        self.sub("Controllers")
        controllers = ("deployment/aws-load-balancer-controller", "deployment/metrics-server",
                       "deployment/coredns", "daemonset/aws-node", "daemonset/kube-proxy")
        for name in controllers:
            path = ("{.status.readyReplicas}|{.spec.replicas}" if name.startswith("deployment/")
                    else "{.status.numberReady}|{.status.desiredNumberScheduled}")
            row = self.kube("get", name, "-n", "kube-system", "-o", f"jsonpath={path}")
            if not row.ok:
                self.fail(f"Could not read {name} in kube-system: {last_line(row.out)}")
                continue
            ready, want = fields(row.out, 2, "|")
            ready = ready or "0"
            if not ready.isdigit() or not want.isdigit():
                self.fail(f"Could not read valid replica counts for {name} in kube-system")
            elif ready == want and int(want) > 0:
                self.ok(f"{name}: {ready}/{want} ready")
            else:
                self.fail(f"{name}: {ready}/{want} ready")

        available = self.kube("get", "apiservice", "v1beta1.metrics.k8s.io", "-o",
                              'jsonpath={.status.conditions[?(@.type=="Available")].status}')
        if not available.ok:
            self.fail(f"Could not read the metrics APIService: {last_line(available.out)}")
        elif available.out == "True":
            self.ok("Metrics API is available")
        else:
            self.fail(f"Metrics API is not available (Available={available.out or 'unknown'}); "
                      "HPAs and kubectl top won't work")

        self.sub("Helm releases")
        if not has_command("helm"):
            self.skip("helm is not installed")
        else:
            releases = run(["helm", "list", "-A"])
            if not releases.ok:
                self.fail(f"Could not list the Helm releases: {last_line(releases.out)}")
            else:
                print(releases.out)
                failed = run(["helm", "list", "-A", "--failed", "--pending", "-q"])
                if not failed.ok:
                    self.fail(f"Could not list failed or pending Helm releases: {last_line(failed.out)}")
                elif failed.out:
                    self.fail(f"Helm releases not deployed: {' '.join(failed.out.splitlines())} ")
                else:
                    self.ok("All Helm releases are deployed")

        self.sub("Load Balancer Controller errors (last 300 log lines per pod)")
        logs = self.kube("logs", "-n", "kube-system", "-l",
                         "app.kubernetes.io/name=aws-load-balancer-controller", "--tail=300", "--prefix")
        if not logs.ok:
            self.warn(f"Could not read the Load Balancer Controller logs: {last_line(logs.out)}")
        elif not logs.out or logs.out.startswith("No resources found"):
            self.warn("No Load Balancer Controller log lines to check; see the controller check above")
        else:
            errors = [line for line in logs.out.splitlines() if '"level":"error"' in line]
            if errors:
                for line in errors[-5:]:
                    print(line[:400])
                self.warn(f"Load Balancer Controller logged {len(errors)} error line(s)")
            else:
                self.ok("No errors in the Load Balancer Controller logs")

    def section_app(self):
        self.section("Application health and metrics")
        if not self.ensure_kube():
            return
        self.sub("Health endpoints (through the Kubernetes API proxy)")
        services = f"/api/v1/namespaces/{self.namespace}/services"
        backend = self.kube("get", "--raw", f"{services}/{self.backend}:http/proxy/health/ready")
        if backend.ok:
            self.ok(f"Backend /health/ready (includes the database check): {backend.out}")
        else:
            self.fail(f"Backend /health/ready failed, so the database or the backend is down: "
                      f"{last_line(backend.out)}")
        frontend = self.kube("get", "--raw", f"{services}/{self.frontend}:http/proxy/health")
        if frontend.ok:
            self.ok(f"Frontend /health: {frontend.out}")
        else:
            self.fail(f"Frontend /health failed: {last_line(frontend.out)}")

        self.sub(f"Backend metrics per pod (port {self.metrics_port}; counters since the pod started, probes included)")
        pods = self.kube("get", "pods", "-n", self.namespace, "-l", f"app.kubernetes.io/name={self.backend}",
                         "--field-selector=status.phase=Running", "-o", "jsonpath={.items[*].metadata.name}")
        if not pods.ok:
            self.warn(f"Could not list the backend pods to read metrics from: {last_line(pods.out)}")
            return
        if not pods.out:
            self.warn("No running backend pods to read metrics from")
            return
        for pod in pods.out.split():
            metrics = self.kube("get", "--raw", f"/api/v1/namespaces/{self.namespace}/pods/"
                                f"{pod}:{self.metrics_port}/proxy/metrics")
            if not metrics.ok:
                self.warn(f"Could not read metrics from {pod}: {last_line(metrics.out)}")
                continue
            print(pod)
            self.metrics_summary(pod, metrics.out)

    def metrics_summary(self, pod, content):
        values = {}
        requests = {}
        for line in content.splitlines():
            if not line or line.startswith("#"):
                continue
            metric, _, raw = line.rpartition(" ")
            name = metric.partition("{")[0]
            try:
                value = float(raw)
            except ValueError:
                continue
            values[name] = values.get(name, 0) + value
            if name == "http_requests_received_total":
                code = re.search(r'code="([0-9]+)"', metric)
                key = code.group(1) if code else "other"
                requests[key] = requests.get(key, 0) + value
        started = values.get("process_start_time_seconds", 0)
        if started:
            elapsed = max(0, int(self.now - started))
            if elapsed >= 86400:
                duration = f"{elapsed // 86400}d {(elapsed % 86400) // 3600}h"
            elif elapsed >= 3600:
                duration = f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"
            else:
                duration = f"{elapsed // 60}m"
            print(f"  {'Uptime':<16} {duration}")
        by_code = "".join(f"{code}={int(count)} " for code, count in sorted(requests.items())) or "none"
        print(f"  {'Requests':<16} {int(sum(requests.values()))} ({by_code})")
        count = values.get("http_request_duration_seconds_count", 0)
        if count:
            average = values.get("http_request_duration_seconds_sum", 0) / count * 1000
            print(f"  {'Avg duration':<16} {average:.0f} ms")
        print(f"  {'In progress':<16} {int(values.get('http_requests_in_progress', 0))}")
        memory = values.get("process_working_set_bytes", 0) / 1048576
        heap = values.get("dotnet_total_memory_bytes", 0) / 1048576
        print(f"  {'Working set':<16} {memory:.0f} MiB (managed heap {heap:.0f} MiB)")
        print(f"  {'CPU time':<16} {values.get('process_cpu_seconds_total', 0):.1f} s, "
              f"{int(values.get('process_num_threads', 0))} threads")
        errors = sum(count for code, count in requests.items() if code.startswith("5"))
        if errors:
            self.warn(f"{pod} answered {int(errors)} request(s) with 5xx since it started")

    def section_events(self):
        self.section("Warning events")
        if not self.ensure_kube():
            return
        for namespace in (self.namespace, "kube-system"):
            self.sub(namespace)
            events = self.kube("get", "events", "-n", namespace, "--field-selector", "type=Warning",
                               "--sort-by=.lastTimestamp", "-o", "custom-columns=LAST:.lastTimestamp,COUNT:.count,"
                               "REASON:.reason,OBJECT:.involvedObject.name,MESSAGE:.message")
            if not events.ok:
                self.warn(f"Could not read the events in {namespace}: {last_line(events.out)}")
            elif not events.out or events.out.startswith("No resources found"):
                self.ok(f"No warning events in {namespace}")
            else:
                lines = events.out.splitlines()
                print(lines[0])
                for line in lines[1:][-25:]:
                    print(line[:240])
                self.warn(f"{len(lines) - 1} warning event(s) in {namespace} "
                          "(Kubernetes keeps events for about an hour)")

    def section_logs(self):
        self.section(f"Application logs (last {self.log_tail_lines} lines per pod)")
        if not self.ensure_kube():
            return
        for deployment in (self.backend, self.frontend):
            self.sub(deployment)
            logs = self.kube("logs", "-n", self.namespace, "-l", f"app.kubernetes.io/name={deployment}",
                             "--all-containers", "--prefix", f"--tail={self.log_tail_lines}", "--max-log-requests=10")
            if not logs.ok:
                self.warn(f"Could not read {deployment} logs: {last_line(logs.out)}")
            elif logs.out.startswith("No resources found"):
                self.warn(f"No {deployment} pods to read logs from")
            elif not logs.out:
                self.note("(no log lines)")
            else:
                print(logs.out)
                count = sum(bool(re.search(r"\b(fail|error|crit|critical|exception|panic|emerg)\b", line, re.I))
                            for line in logs.out.splitlines())
                if count:
                    self.warn(f"{deployment}: {count} of the recent log lines mention errors or exceptions")
                else:
                    self.ok(f"{deployment}: no errors in the recent log lines")
        self.sub("Logs from before the last restart")
        restarts = self.kube("get", "pods", "-n", self.namespace, "-o",
                             'jsonpath={range .items[*]}{.metadata.name}{" "}'
                             '{.status.containerStatuses[*].restartCount}{"\\n"}{end}')
        if not restarts.ok:
            self.warn(f"Could not read the container restart counts: {last_line(restarts.out)}")
            return
        names = []
        for line in restarts.out.splitlines():
            parts = line.split()
            if parts and sum(int(n) for n in parts[1:] if n.isdigit()) > 0:
                names.append(parts[0])
        for name in names:
            print(name)
            previous = self.kube("logs", "-n", self.namespace, name, "--all-containers", "--previous",
                                 "--prefix", "--tail=50")
            print("\n".join("  " + line for line in previous.out.splitlines()))
        if not names:
            self.note("No restarted containers.")

    def section_control_plane(self):
        self.section(f"EKS control plane logs (last {self.lookback_hours}h)")
        group = f"/aws/eks/{self.cluster}/cluster"
        info = self.aws("logs", "describe-log-groups", "--log-group-name-prefix", group, "--query",
                        f"logGroups[?logGroupName=='{group}'].[retentionInDays, storedBytes]", "--output", "text")
        if not info.ok:
            self.fail(f"Could not read log group {group}: {last_line(info.out)}")
            return
        if not info.out:
            self.warn(f"Log group {group} does not exist")
            return
        retention, stored = fields(info.out, 2, separator=None)
        types = self.aws("eks", "describe-cluster", "--name", self.cluster, "--query",
                         "cluster.logging.clusterLogging[?enabled].types[]", "--output", "text", stderr=DROP)
        enabled = types.out.replace("\t", " ") if types.ok and types.out else "none"
        print(f"{'Group':<10} {group}\n{'Retention':<10} {retention} days\n{'Stored':<10} "
              f"{float(stored or 0) / 1048576:.1f} MiB\n{'Types':<10} {enabled}")
        for prefix, pattern in (("authenticator", '"access denied"'),
                                ("kube-controller-manager", "?error ?Error ?failed ?Failed"),
                                ("kube-scheduler", "?error ?Error ?failed ?Failed")):
            self.sub(f"{prefix}: {pattern}")
            events = self.aws("logs", "filter-log-events", "--log-group-name", group,
                              "--log-stream-name-prefix", prefix, "--start-time", str(self.window_start * 1000),
                              "--filter-pattern", pattern, "--max-items", "1000", "--query", "events[].[message]",
                              "--output", "text")
            if not events.ok:
                self.warn(f"Could not search {prefix} logs: {last_line(events.out)}")
                continue
            lines = [line for line in events.out.splitlines() if line and not line.startswith("NEXTTOKEN")]
            if not lines:
                self.ok(f"{prefix}: nothing matching")
            else:
                for line in lines[-10:]:
                    print(line[:300])
                self.warn(f"{prefix}: {len(lines)} matching line(s)")

    def section_ecr(self):
        self.section("Container images (ECR)")
        connected = self.ensure_kube()
        for url, deployment in ((self.backend_repository_url, self.backend),
                                (self.frontend_repository_url, self.frontend)):
            if not url:
                self.skip(f"No repository URL for {deployment}")
                continue
            repo = url.rsplit("/", 1)[-1]
            self.sub(f"{repo} (newest first)")
            images = self.aws("ecr", "describe-images", "--repository-name", repo, "--query",
                              "reverse(sort_by(imageDetails, &imagePushedAt))[:5].[imagePushedAt, "
                              "imageScanStatus.status || `NONE`, imageScanFindingsSummary.findingSeverityCounts.CRITICAL "
                              "|| `0`, imageScanFindingsSummary.findingSeverityCounts.HIGH || `0`, "
                              "join(`,`, imageTags || `[]`)]", "--output", "text")
            if not images.ok:
                self.fail(f"Could not list images in {repo}: {last_line(images.out)}")
                continue
            if not images.out:
                self.fail(f"{repo} has no images")
                continue
            print(f"{'PUSHED':<20} {'SCAN':<10} {'CRITICAL':<9} {'HIGH':<5} TAGS")
            for line in images.out.splitlines():
                pushed, scan, critical, high, tags = fields(line, 5)
                print(f"{pushed[:19]:<20} {scan:<10} {critical:<9} {high:<5} {tags}")
            newest = fields(images.out.splitlines()[0], 5)[4]
            if not connected:
                self.note("Not connected to the cluster, so the running image isn't compared.")
                continue
            running = self.kube("get", "deployment", deployment, "-n", self.namespace,
                                "-o", "jsonpath={.spec.template.spec.containers[0].image}")
            if not running.ok:
                self.warn(f"Could not read the image {deployment} runs: {last_line(running.out)}")
                continue
            if not running.out:
                self.warn(f"Deployment {deployment} has no container image")
                continue
            tag = running.out.rsplit(":", 1)[-1]
            if tag in newest.split(","):
                self.ok(f"{deployment} runs the newest image ({tag})")
            else:
                self.warn(f"{deployment} runs {tag} but the newest {repo} image is tagged {newest or 'untagged'}")
            check_image(self, deployment, self.namespace, self.github_repository)
            scan = self.aws("ecr", "describe-images", "--repository-name", repo, "--image-ids", f"imageTag={tag}",
                            "--query", "imageDetails[0].[imageScanStatus.status || `NONE`, "
                            "imageScanFindingsSummary.findingSeverityCounts.CRITICAL || `0`, "
                            "imageScanFindingsSummary.findingSeverityCounts.HIGH || `0`]", "--output", "text")
            if not scan.ok:
                self.warn(f"Could not read the scan results for {deployment} image {tag}: {last_line(scan.out)}")
                continue
            status, critical, high = fields(scan.out, 3, separator=None)
            if (as_int(critical) or 0) > 0 or (as_int(high) or 0) > 0:
                self.warn(f"Running {deployment} image {tag} has {critical} critical and {high} high scan findings")
            elif status:
                self.ok(f"Running {deployment} image {tag}: scan {status}, no critical or high findings")

    def section_cost(self):
        self.section("Month-to-date spend (credits and refunds excluded)")
        start = utc_now("%Y-%m-01")
        end = datetime.fromtimestamp(self.now + 86400, timezone.utc).strftime("%Y-%m-%d")
        rows = run(["aws", "ce", "get-cost-and-usage", "--time-period", f"Start={start},End={end}",
                    "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "--group-by",
                    "Type=DIMENSION,Key=SERVICE", "--filter",
                    '{"Not":{"Dimensions":{"Key":"RECORD_TYPE","Values":["Credit","Refund"]}}}',
                    "--query", "ResultsByTime[].Groups[].[Keys[0], Metrics.UnblendedCost.Amount]",
                    "--output", "text"])
        if not rows.ok:
            self.fail(f"Cost Explorer query failed: {last_line(rows.out)}")
            return
        amounts = []
        for line in rows.out.splitlines():
            service, amount = fields(line, 2)
            try:
                amounts.append((service, float(amount)))
            except ValueError:
                self.warn(f"Could not parse a Cost Explorer row: {line}")
        for service, amount in sorted(amounts, key=lambda row: row[1], reverse=True):
            if amount >= .005:
                print(f"{service:<52} ${amount:9.2f}")
        total = sum(amount for _, amount in amounts)
        print(f"{'Total':<52} ${total:9.2f}")
        self.ok(f"Spend from {start} to today: ${total:.2f} before credits")
        self.note("Cost Explorer bills $0.01 per request; this section makes one.")

    def section_cicd(self):
        self.section("Deploy pipeline")
        self.sub(f"SSM deploy commands ({self.deploy_ssm_document})")
        doc = self.aws("ssm", "describe-document", "--name", self.deploy_ssm_document,
                       "--query", "Document.Status", "--output", "text")
        if not doc.ok:
            self.fail(f"Could not read deploy document {self.deploy_ssm_document}: {last_line(doc.out)}")
        elif doc.out == "Active":
            self.ok(f"Deploy document {self.deploy_ssm_document} is Active")
        else:
            self.fail(f"Deploy document {self.deploy_ssm_document} is {doc.out or 'missing'}")
        commands = self.aws("ssm", "list-commands", "--filters", f"key=DocumentName,value={self.deploy_ssm_document}",
                            "--query", "Commands[].[RequestedDateTime, Status, Parameters.ImageTag[0], CommandId]",
                            "--output", "text")
        if not commands.ok:
            self.fail(f"Could not list the deploy commands: {last_line(commands.out)}")
        elif not commands.out:
            self.note("No deploy commands in the last 30 days.")
        else:
            recent = sorted(commands.out.splitlines(), reverse=True)[:5]
            for line in recent:
                requested, status, tag, command_id = fields(line, 4)
                print(f"{requested[:19]:<20} {status:<10} {tag}")
            requested, status, tag, command_id = fields(recent[0], 4)
            if status == "Success":
                self.ok(f"Last deploy ({tag}) succeeded at {requested.split('.', 1)[0]}")
            elif status in ("Pending", "InProgress", "Delayed"):
                self.note(f"Deploy of {tag} is {status}.")
            else:
                self.fail(f"Last deploy ({tag}) ended as {status}")
                output = self.aws("ssm", "list-command-invocations", "--command-id", command_id, "--details",
                                  "--query", "CommandInvocations[0].CommandPlugins[0].Output", "--output", "text",
                                  stderr=DROP)
                print_tail(output.out, 15)

        self.sub(f"GitHub Actions ({self.github_repository})")
        if not has_command("gh"):
            self.skip("gh is not installed")
            return
        if not run(["gh", "auth", "status"], stderr=DROP).ok:
            self.skip("gh is not logged in (gh auth login)")
            return
        runs = run(["gh", "run", "list", "-R", self.github_repository, "-L", "10"])
        print(runs.out)
        if not runs.ok:
            self.warn("Could not list workflow runs")
        workflow_dir = REPO_ROOT / "CICD/.github/workflows"
        for path in workflow_dir.glob("*.yml"):
            name = next((line.split(":", 1)[1].strip() for line in path.read_text().splitlines()
                         if line.startswith("name:")), "")
            if not name:
                continue
            latest = run(["gh", "run", "list", "-R", self.github_repository, "-w", name, "-L", "1",
                          "--json", "status,conclusion,createdAt,headSha,displayTitle", "-q",
                          ".[] | [.status, .conclusion, .createdAt, .headSha[0:7], .displayTitle] | @tsv"])
            if not latest.ok:
                self.warn(f"Could not read the latest {name} run: {last_line(latest.out)}")
            elif not latest.out:
                self.note(f"{name}: no runs")
            else:
                status, conclusion, created, sha, title = fields(latest.out, 5)
                if status != "completed":
                    self.note(f"{name} is {status} for {sha} ({title}).")
                elif conclusion in ("success", "skipped"):
                    self.ok(f"{name}: {conclusion} for {sha} at {created}")
                else:
                    self.fail(f"{name}: {conclusion} for {sha} at {created} ({title})")

        self.sub("Repository variables vs Terraform outputs")
        if not has_command("terraform"):
            self.skip("terraform is needed to compare the repository variables")
            return
        output = run(["terraform", f"-chdir={REPO_ROOT / 'terraform'}", "output", "-json",
                      "github_actions_variables"], stderr=DROP)
        if not output.ok:
            self.skip("Terraform state has no github_actions_variables output")
            return
        try:
            expected = json.loads(output.out)
            if not isinstance(expected, dict):
                raise ValueError("expected a mapping")
        except (ValueError, TypeError):
            self.skip("Terraform state has no github_actions_variables output")
            return
        actual = {}
        for scope, args in ((self.github_deploy_environment, ["--env", self.github_deploy_environment]),
                            ("repository", [])):
            variables = run(["gh", "variable", "list", "-R", self.github_repository, *args,
                             "--json", "name,value", "-q", ".[] | [.name, .value] | @tsv"])
            if not variables.ok:
                self.warn(f"Could not list the {scope} variables: {last_line(variables.out)}")
                return
            for line in variables.out.splitlines():
                key, value = fields(line, 2)
                actual.setdefault(key, value)
        missing = mismatches = 0
        for key, want in expected.items():
            want = want if isinstance(want, str) else json.dumps(want, separators=(",", ":"))
            if key not in actual or not actual[key]:
                missing += 1
                self.warn(f"{key} is not a repository or {self.github_deploy_environment} variable "
                          f"(fine if it is a secret); Terraform expects {want}")
            elif actual[key] != want:
                mismatches += 1
                self.fail(f"{key} is {actual[key]} but Terraform expects {want}")
        if not missing and not mismatches:
            self.ok("GitHub variables match the Terraform outputs")

    def usage_vs_limits(self):
        usage = self.kube("top", "pods", "-n", self.namespace, "--no-headers")
        if not usage.ok:
            self.warn(f"kubectl top pods failed: {last_line(usage.out)}")
            return
        limits = self.kube("get", "pods", "-n", self.namespace, "--no-headers", "-o",
                           "custom-columns=NAME:.metadata.name,CPU:.spec.containers[0].resources.limits.cpu,"
                           "MEMORY:.spec.containers[0].resources.limits.memory")
        if not limits.ok:
            self.warn(f"Could not read the pod limits: {last_line(limits.out)}")
            return

        def cpu(value):
            try:
                return float(value[:-1]) if value.endswith("m") else float(value) * 1000
            except ValueError:
                return -1

        def memory(value):
            try:
                if value.endswith("Ki"):
                    return float(value[:-2]) / 1024
                if value.endswith("Mi"):
                    return float(value[:-2])
                if value.endswith("Gi"):
                    return float(value[:-2]) * 1024
                return float(value) / 1048576
            except ValueError:
                return -1

        limits_by_pod = {}
        for line in limits.out.splitlines():
            name, cpu_limit, memory_limit = fields(line, 3, separator=None)
            limits_by_pod[name] = (cpu_limit, memory_limit)
        print(f"{'POD':<44} {'CPU':>8} {'LIMIT':>8} {'USE':>5} {'MEMORY':>9} {'LIMIT':>9} {'USE':>5}")
        for line in usage.out.splitlines():
            name, used_cpu, used_memory = fields(line, 3, separator=None)
            max_cpu, max_memory = limits_by_pod.get(name, ("", ""))
            cpu_pct = int(cpu(used_cpu) * 100 / cpu(max_cpu)) if cpu(max_cpu) > 0 and cpu(used_cpu) >= 0 else -1
            mem_pct = (int(memory(used_memory) * 100 / memory(max_memory))
                       if memory(max_memory) > 0 and memory(used_memory) >= 0 else -1)
            print(f"{name:<44} {used_cpu:>8} {max_cpu:>8} {str(cpu_pct) + '%' if cpu_pct >= 0 else '-':>5} "
                  f"{used_memory:>9} {max_memory:>9} {str(mem_pct) + '%' if mem_pct >= 0 else '-':>5}")
            if mem_pct >= 90:
                self.warn(f"{name} uses {mem_pct}% of its memory limit; it may be OOM-killed")
            if cpu_pct >= 90:
                self.warn(f"{name} uses {cpu_pct}% of its CPU limit and is being throttled")

    def section_workloads(self):
        self.section(f"Application workloads ({self.namespace})")
        if not self.ensure_kube():
            return
        namespace = self.kube("get", "namespace", self.namespace, "-o", "name")
        if not namespace.ok:
            self.fail(f"Could not read namespace {self.namespace}: {last_line(namespace.out)}")
            return

        self.sub("Deployments")
        print(self.kube("get", "deployments", "-n", self.namespace, "-o", "wide").out)
        for deployment in (self.backend, self.frontend):
            row = self.kube("get", "deployment", deployment, "-n", self.namespace, "-o",
                            "jsonpath={.spec.replicas}|{.status.readyReplicas}|{.status.updatedReplicas}|"
                            "{.spec.template.spec.containers[0].image}")
            if not row.ok:
                self.fail(f"Could not read Deployment {deployment}: {last_line(row.out)}")
                continue
            desired, ready, updated, image = fields(row.out, 4, "|")
            ready = ready or "0"
            if not desired.isdigit() or not ready.isdigit() or not (updated or "0").isdigit() or not image:
                self.fail(f"Could not read valid replica counts and image for Deployment {deployment}")
                continue
            if desired == "0":
                self.warn(f"{deployment} is scaled to 0")
            elif int(ready) < int(desired):
                self.fail(f"{deployment}: {ready}/{desired} ready, {updated or '0'} updated")
            else:
                self.ok(f"{deployment}: {ready}/{desired} ready on {image.rsplit(':', 1)[-1]}")
            status = self.kube("rollout", "status", f"deployment/{deployment}", "-n", self.namespace,
                               "--watch=false")
            if not status.ok:
                if "exceeded its progress deadline" in status.out:
                    self.fail(f"{deployment} rollout exceeded its progress deadline; roll back with kubectl "
                              f"rollout undo deployment/{deployment} -n {self.namespace}")
                else:
                    self.fail(f"Could not read the rollout status of {deployment}: {last_line(status.out)}")
            elif "successfully rolled out" not in status.out:
                self.warn(f"{deployment} rollout: {last_line(status.out)}")

        self.sub("Rollout history (ReplicaSets, oldest first)")
        for deployment in (self.backend, self.frontend):
            history = self.kube("get", "replicasets", "-n", self.namespace, "-l",
                                f"app.kubernetes.io/name={deployment}", "--sort-by=.metadata.creationTimestamp",
                                "-o", "custom-columns=REVISION:.metadata.annotations.deployment\\.kubernetes\\.io/revision,"
                                "DESIRED:.spec.replicas,READY:.status.readyReplicas,CREATED:.metadata.creationTimestamp,"
                                "IMAGE:.spec.template.spec.containers[0].image")
            print(deployment)
            lines = history.out.splitlines()
            if lines:
                print(lines[0])
                if len(lines) > 1:
                    print("\n".join(lines[1:][-5:]))

        self.sub("Pods")
        print(self.kube("get", "pods", "-n", self.namespace, "-o", "wide").out)
        self.check_pods(self.namespace)

        self.sub("Autoscaling")
        print(self.kube("get", "hpa", "-n", self.namespace).out)
        hpas = self.kube("get", "hpa", "-n", self.namespace, "-o",
                         'jsonpath={range .items[*]}{.metadata.name}{"|"}{.status.currentReplicas}{"|"}'
                         '{.spec.maxReplicas}{"|"}{.status.conditions[?(@.type=="ScalingActive")].status}'
                         '{"\\n"}{end}')
        if not hpas.ok:
            self.warn(f"Could not read the HPA status: {last_line(hpas.out)}")
        elif not hpas.out:
            self.warn(f"No HorizontalPodAutoscalers in {self.namespace}")
        else:
            for line in hpas.out.splitlines():
                name, current, maximum, active = fields(line, 4, "|")
                if name and active != "True":
                    self.warn(f"HPA {name} can't compute its CPU metric (ScalingActive={active or 'unknown'})")
                if name and current and current == maximum:
                    self.warn(f"HPA {name} is at its maximum of {maximum} replicas")

        self.sub("Usage vs limits")
        self.usage_vs_limits()
        self.sub("Services and endpoints")
        print(self.kube("get", "services", "-n", self.namespace, "-o", "wide").out)
        services = self.kube("get", "services", "-n", self.namespace, "-o", "jsonpath={.items[*].metadata.name}")
        if not services.ok:
            self.fail(f"Could not list the services in {self.namespace}: {last_line(services.out)}")
        elif not services.out:
            self.fail(f"No services in {self.namespace}")
        for service in services.out.split() if services.ok else ():
            endpoints = self.kube("get", "endpointslices", "-n", self.namespace, "-l",
                                  f"kubernetes.io/service-name={service}", "-o",
                                  'jsonpath={range .items[*].endpoints[*]}{.conditions.ready}{"\\n"}{end}')
            if not endpoints.ok:
                self.fail(f"Could not read the endpoints of service {service}: {last_line(endpoints.out)}")
                continue
            count = sum(line == "true" for line in endpoints.out.splitlines())
            if count:
                self.ok(f"Service {service} has {count} ready endpoint(s)")
            else:
                self.fail(f"Service {service} has no ready endpoints")

        self.sub("Ingress")
        print(self.kube("get", "ingress", self.ingress, "-n", self.namespace, "-o", "wide").out)
        hostname = self.kube("get", "ingress", self.ingress, "-n", self.namespace, "-o",
                             "jsonpath={.status.loadBalancer.ingress[0].hostname}")
        if not hostname.ok:
            self.fail(f"Could not read Ingress {self.ingress}: {last_line(hostname.out)}")
        elif hostname.out:
            self.ok(f"Ingress {self.ingress} has ALB {hostname.out}")
        else:
            self.fail(f"Ingress {self.ingress} has no ALB hostname; check the Load Balancer Controller")
        bindings = self.kube("get", "targetgroupbindings", "-n", self.namespace, "-o", "wide")
        print(bindings.out)
        if not bindings.ok:
            self.warn(f"Could not list the TargetGroupBindings in {self.namespace}")

        self.sub("Configuration")
        refs = set()
        for deployment in (self.backend, self.frontend):
            row = self.kube("get", "deployment", deployment, "-n", self.namespace, "-o",
                            'jsonpath={range .spec.template.spec.containers[*].envFrom[*]}'
                            'configmap/{.configMapRef.name}{"\\n"}secret/{.secretRef.name}{"\\n"}{end}')
            if not row.ok:
                self.fail(f"Could not read the ConfigMap and Secret references of {deployment}: {last_line(row.out)}")
                continue
            refs.update(ref for ref in row.out.splitlines() if ref and not ref.endswith("/"))
        for ref in sorted(refs):
            answer = self.kube("get", ref, "-n", self.namespace, "-o", "name")
            if not answer.ok:
                self.fail(f"{ref} is referenced by a Deployment but could not be read: {last_line(answer.out)}")
            elif ref.startswith("secret/"):
                keys = self.kube("get", ref, "-n", self.namespace, "-o",
                                 "go-template={{range $k, $v := .data}}{{$k}} {{end}}")
                if keys.ok:
                    self.ok(f"{ref} exists with keys: {keys.out or 'none'}")
                else:
                    self.warn(f"{ref} exists but its keys could not be read: {last_line(keys.out)}")
            else:
                self.ok(f"{ref} exists")

        external = self.kube("get", "externalsecret", "hospital-backend-secrets", "-n", self.namespace,
                             "-o", 'jsonpath={.status.conditions[?(@.type=="Ready")].status}|'
                             '{.status.conditions[?(@.type=="Ready")].message}')
        if not external.ok:
            self.fail(f"Could not read ExternalSecret hospital-backend-secrets: {last_line(external.out)}")
        elif external.out.startswith("True|"):
            self.ok("ExternalSecret hospital-backend-secrets is synced from Secrets Manager")
        else:
            self.fail(f"ExternalSecret hospital-backend-secrets isn't synced: {external.out.partition('|')[2]}")
        secret_hash = self.kube("get", "secret", "hospital-backend-secrets", "-n", self.namespace, "-o",
                                r"jsonpath={.metadata.annotations.reconcile\.external-secrets\.io/data-hash}",
                                stderr=DROP).out
        started_with = self.kube("get", "deployment", self.backend, "-n", self.namespace, "-o",
                                 r"jsonpath={.metadata.annotations.hospitalsystem\.io/backend-secrets-hash}",
                                 stderr=DROP).out
        if not secret_hash or not started_with:
            self.warn("Can't tell which backend Secret the pods use; run ansible-playbook "
                      "ansible/playbooks/backend-secret.yml")
        elif secret_hash == started_with:
            self.ok("Backend pods started with the current Secret")
        else:
            self.warn("The backend Secret changed after its pods started; run ansible-playbook "
                      "ansible/playbooks/backend-secret.yml to restart them")

        for policy in (f"{self.namespace}-trusted-workloads", f"{self.namespace}-deploy-image-only"):
            policy_read = self.kube("get", "validatingadmissionpolicy", policy, "-o", "name")
            binding_read = self.kube("get", "validatingadmissionpolicybinding", policy, "-o", "name")
            if not policy_read.ok:
                self.fail(f"Could not read admission policy {policy}: {last_line(policy_read.out)}")
            elif not binding_read.ok:
                self.fail(f"Could not read admission policy binding {policy}: {last_line(binding_read.out)}")
            else:
                self.ok(f"Admission policy {policy} and its binding are in place")

        allowed = self.kube("get", "networkpolicy", "allow-alb-and-api-server", "-n", self.namespace, "-o",
                            'jsonpath={range .spec.ingress[*].from[*]}{.ipBlock.cidr}{"\\n"}{end}')
        if not allowed.ok:
            self.fail(f"Could not read NetworkPolicy allow-alb-and-api-server: {last_line(allowed.out)}")
        else:
            enis = self.aws("ec2", "describe-network-interfaces", "--filters",
                            f"Name=description,Values=Amazon EKS {self.cluster}", "--query",
                            "NetworkInterfaces[].PrivateIpAddress", "--output", "text")
            if not enis.ok:
                self.warn(f"Could not list the EKS control plane ENIs: {last_line(enis.out)}")
            elif enis.out in ("", "None"):
                self.warn("No EKS control plane ENIs found; the NetworkPolicy could not be checked")
            else:
                missing = [ip for ip in enis.out.split() if f"{ip}/32" not in allowed.out.splitlines()]
                if missing:
                    self.fail("NetworkPolicy allow-alb-and-api-server doesn't allow the control plane at "
                              f"{' '.join(missing)}; deploy smoke tests will fail. Run ansible-playbook "
                              "ansible/playbooks/apply-kubernetes.yml")
                else:
                    self.ok("NetworkPolicy allows the ALB subnets and the current control plane ENIs")

        if self.deploy_group:
            for deployment in (self.backend, self.frontend):
                can_patch = self.kube("auth", "can-i", "patch", f"deployments.apps/{deployment}",
                                      "-n", self.namespace, "--as=monitoring-check", f"--as-group={self.deploy_group}")
                answer = last_line(can_patch.out)
                if can_patch.ok and answer == "yes":
                    self.ok(f"Deploy group {self.deploy_group} can patch {deployment}")
                elif answer.startswith("no"):
                    self.fail(f"Deploy group {self.deploy_group} can't patch {deployment}; CI/CD deploys will fail")
                else:
                    self.fail(f"Could not check whether {self.deploy_group} can patch {deployment}: {answer}")
            other = self.kube("auth", "can-i", "patch", "deployments.apps/monitoring-check", "-n",
                              self.namespace, "--as=monitoring-check", f"--as-group={self.deploy_group}")
            answer = last_line(other.out)
            if other.ok and answer == "yes":
                self.warn(f"Deploy group {self.deploy_group} can patch any Deployment in {self.namespace}, "
                          "not just the two apps")
            elif answer.startswith("no"):
                self.ok(f"Deploy group {self.deploy_group} can't patch other Deployments")
            else:
                self.warn(f"Could not check whether {self.deploy_group} can patch other Deployments: {answer}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh-alarms", action="store_true", help="recreate the ALB alarms before reading them")
    parser.add_argument("sections", nargs="*", choices=ALL_SECTIONS, metavar="section",
                        help="checks to run, in order; default: all")
    args = parser.parse_args()
    for name in args.sections:
        if name not in ALL_SECTIONS:
            parser.error(f"unknown section: {name}")
    selected = args.sections or ALL_SECTIONS
    settings = {
        "AWS_REGION": setting("AWS_REGION", "aws_region"),
        "EKS_CLUSTER_NAME": setting("EKS_CLUSTER_NAME", "eks_cluster_name"),
        "NAMESPACE": setting("NAMESPACE", "k8s_namespace"),
        "BACKEND_DEPLOYMENT": setting("BACKEND_DEPLOYMENT", "backend_deployment"),
        "FRONTEND_DEPLOYMENT": setting("FRONTEND_DEPLOYMENT", "frontend_deployment"),
        "INGRESS_NAME": setting("INGRESS_NAME", "ingress_name"),
        "ALB_NAME": setting("ALB_NAME", "alb_name"),
        "ALARM_PREFIX": setting("ALARM_PREFIX", "monitoring_alarm_prefix"),
        "APP_DOMAIN": setting("APP_DOMAIN", "app_domain_name"),
        "BACKEND_METRICS_PORT": setting("BACKEND_METRICS_PORT", "backend_metrics_port", "9091"),
        "LOG_TAIL_LINES": setting("LOG_TAIL_LINES", default="100"),
        "LOOKBACK_HOURS": setting("LOOKBACK_HOURS", default="3"),
    }
    required = ("AWS_REGION", "EKS_CLUSTER_NAME", "NAMESPACE", "BACKEND_DEPLOYMENT", "FRONTEND_DEPLOYMENT",
                "INGRESS_NAME", "ALB_NAME", "ALARM_PREFIX", "APP_DOMAIN")
    for name in required:
        if not settings[name]:
            raise ScriptError(f"Missing {name}: export it, or run ./scripts/tf.py apply to generate "
                              f"{group_vars_dir()}/terraform.yml")
    for name in ("BACKEND_METRICS_PORT", "LOG_TAIL_LINES", "LOOKBACK_HOURS"):
        if not re.fullmatch(r"[1-9][0-9]*", settings[name]):
            raise ScriptError(f"{name} must be a positive whole number, got: {settings[name]}")
    os.environ["AWS_PAGER"] = ""
    os.environ["KUBECONFIG"] = str(REPO_ROOT / ".generated/kubeconfig")
    monitor = Monitor(settings, args.refresh_alarms)
    require_command("aws")
    identity = run(["aws", "sts", "get-caller-identity", "--query", "[Account, Arn]", "--output", "text"],
                   stderr=DROP)
    if not identity.ok:
        raise ScriptError("No usable AWS credentials. Run: aws login")
    account, caller = fields(identity.out, 2)
    monitor.section("HospitalSystem monitoring")
    for name, value in (("Time", utc_now("%Y-%m-%d %H:%M:%S UTC")), ("Account", account), ("Caller", caller),
                        ("Region", monitor.region), ("Cluster", monitor.cluster), ("Namespace", monitor.namespace),
                        ("App", monitor.app_url), ("Window", f"last {monitor.lookback_hours}h"),
                        ("Sections", " ".join(selected))):
        print(f"{name:<10} {value}")
    sections = [(name, getattr(monitor, f"section_{name.replace('-', '_')}")) for name in selected]
    return 0 if monitor.run_sections(sections) else 1


if __name__ == "__main__":
    entrypoint(main)
