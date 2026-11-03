#!/usr/bin/env python3
"""Check whether the rolling deployment is set up correctly and, on request,
whether a real rollout serves every request. Exits 1 when any check fails.

./scripts/rollout-check.py             # settings only; changes nothing
./scripts/rollout-check.py --watch     # probes the app while a CI deploy rolls out
./scripts/rollout-check.py --restart   # starts a rollout itself and probes it

With no options it only reads settings and changes nothing:
  - the RollingUpdate strategy, surge and shutdown settings on both Deployments
  - the ALB readiness gate on the namespace, the pods and the controller webhook
  - the target group drain time, in the Ingress and as the ALB has it
  - whether the nodes have room for the extra pod each rollout starts

During a rollout the script records, for every sample: how many pods each
Deployment has, how many are ready, and the HTTP code from the frontend and
from the backend through the ALB. It fails if a request is refused or answers
5xx, if a Deployment ever drops to zero ready pods, or if the Deployments
could not be read in any sample, since their ready pods are then unknown.

Environment overrides: AWS_REGION, NAMESPACE, BACKEND_DEPLOYMENT,
FRONTEND_DEPLOYMENT, INGRESS_NAME, ALB_NAME, APP_DOMAIN, KUBE_TIMEOUT (30s),
PROBE_INTERVAL (1), PROBE_TIMEOUT (3), ROLLOUT_TIMEOUT (600), NO_COLOR.
"""

import argparse
from collections import Counter
import os
import re
import time

from lib.checks import Checks, utc_now
from lib.commands import (CAPTURE, DROP, ScriptError, entrypoint, fields, has_command, last_line, print_tail,
                          require_command, run)
from lib.config import REPO_ROOT, group_vars_dir, setting

# One read of a Deployment: generation|observed|spec|pods|updated|ready|available.
DEPLOYMENT_STATE = ("jsonpath={.metadata.generation}|{.status.observedGeneration}|{.spec.replicas}"
                    "|{.status.replicas}|{.status.updatedReplicas}|{.status.readyReplicas}"
                    "|{.status.availableReplicas}")
# The API server always sets generation and spec.replicas. The status counts
# are left out while they are 0.
STATE_ROW = re.compile(r"[0-9]+\|[0-9]*\|[0-9]+(\|[0-9]*){4}")
ROLLING_UPDATE = ("jsonpath={.spec.strategy.type}|{.spec.strategy.rollingUpdate.maxSurge}"
                  "|{.spec.strategy.rollingUpdate.maxUnavailable}|{.spec.minReadySeconds}"
                  "|{.spec.progressDeadlineSeconds}|{.spec.revisionHistoryLimit}"
                  "|{.spec.template.spec.terminationGracePeriodSeconds}"
                  "|{.spec.template.spec.containers[0].lifecycle.preStop.sleep.seconds}|{.spec.replicas}")


def now():
    return int(time.time())


def first_read_error(state):
    """The first "deployment: error" in deployment_state() output."""
    for line in state.split("\n"):
        if "|unreadable|" in line:
            return line.replace("|unreadable|", ": ", 1)
    return ""


class RolloutCheck(Checks):
    def __init__(self, settings, mode, interval, probe_timeout, rollout_timeout):
        super().__init__(settings["AWS_REGION"], setting("KUBE_TIMEOUT", default="30s"), name_width=10)
        self.namespace = settings["NAMESPACE"]
        self.deployments = (settings["BACKEND_DEPLOYMENT"], settings["FRONTEND_DEPLOYMENT"])
        self.ingress = settings["INGRESS_NAME"]
        self.alb_name = settings["ALB_NAME"]
        self.app_url = f"https://{settings['APP_DOMAIN']}"
        self.frontend_url = f"{self.app_url}/health"
        self.backend_url = f"{self.app_url}/api/health"
        self.mode = mode
        self.interval = interval
        self.probe_timeout = probe_timeout
        self.rollout_timeout = rollout_timeout

    def probe(self, url):
        """The HTTP code and content type as code|type, or 000| when the request
        never answered."""
        answer = run(["curl", "-sS", "-o", "/dev/null", "--max-time", self.probe_timeout,
                      "-w", "%{http_code}|%{content_type}", url], stderr=DROP).out
        return answer or "000|"

    def deployment_state(self):
        """One line per Deployment, name|generation|observed|spec|pods|updated|ready|available,
        and whether both could be read.

        A Deployment that couldn't be read gets name|unreadable|error instead,
        so a failed read never looks like 0 pods wanted.
        """
        lines = []
        readable = True
        for deployment in self.deployments:
            result = self.kube("get", "deployment", deployment, "-n", self.namespace, "-o", DEPLOYMENT_STATE,
                               stderr=CAPTURE)
            row = result.out
            if not result.ok:
                row = f"unreadable|{last_line(result.err) or 'kubectl get deployment failed'}"
                readable = False
            elif not STATE_ROW.fullmatch(row):
                row = f"unreadable|unexpected kubectl output: {row}"
                readable = False
            lines.append(f"{deployment}|{row}")
        return "\n".join(lines), readable

    def section_settings(self):
        self.section("Rolling update settings")
        if not self.ensure_kube():
            return
        for deployment in self.deployments:
            self.sub(deployment)
            row = self.kube("get", "deployment", deployment, "-n", self.namespace, "-o", ROLLING_UPDATE)
            if not row.ok:
                self.fail(f"Deployment {deployment} not found: {last_line(row.out)}")
                continue
            kind, surge, unavailable, min_ready, deadline, history, grace, sleep_for, replicas = fields(row.out, 9, "|")

            if kind == "RollingUpdate":
                self.ok(f"{deployment} uses RollingUpdate")
            else:
                self.fail(f"{deployment} uses {kind or 'an unset strategy'}; every deploy takes the app down. "
                          "Apply the manifests with ansible/playbooks/apply-kubernetes.yml")
                continue

            if unavailable in ("0", "0%"):
                self.ok(f"{deployment} keeps every replica up during a rollout (maxUnavailable {unavailable})")
            elif not unavailable:
                self.warn(f"{deployment} has no maxUnavailable; the default 25% rounds down to 0 for {replicas} "
                          "replica(s), but set it to 0 to be explicit")
            else:
                self.fail(f"{deployment} has maxUnavailable {unavailable}, so a rollout may take the last pod down "
                          "before the new one is ready")

            if not surge:
                self.warn(f"{deployment} has no maxSurge; the default 25% rounds up to 1 pod, but set it to be explicit")
            elif surge in ("0", "0%"):
                self.fail(f"{deployment} has maxSurge 0, so no new pod can start before the old one stops")
            else:
                self.ok(f"{deployment} starts {surge} extra pod(s) during a rollout (maxSurge {surge})")

            if not sleep_for:
                self.warn(f"{deployment} has no preStop hook; a stopping pod can be killed while the ALB still sends "
                          "it requests")
            elif not grace:
                self.warn(f"{deployment} waits {sleep_for}s in preStop but has no terminationGracePeriodSeconds")
            elif int(grace) <= int(sleep_for):
                self.fail(f"{deployment} waits {sleep_for}s in preStop but is killed after {grace}s, leaving no time "
                          "to shut down")
            else:
                self.ok(f"{deployment} drains for {sleep_for}s, then has {int(grace) - int(sleep_for)}s to shut down")

            for name, value in (("replicas", replicas), ("minReadySeconds", min_ready or "0"),
                                ("progressDeadlineSeconds", deadline or "600"),
                                ("revisionHistoryLimit", history or "10")):
                print(f"{name:<28} {value}")

    def section_gates(self):
        self.section("ALB readiness gate and drain time")
        if not self.ensure_kube():
            return
        label = self.kube("get", "namespace", self.namespace, "-o",
                          r"jsonpath={.metadata.labels.elbv2\.k8s\.aws/pod-readiness-gate-inject}", stderr=DROP).out
        if label == "enabled":
            self.ok(f"Namespace {self.namespace} asks the controller to add readiness gates")
        else:
            self.fail(f"Namespace {self.namespace} is missing elbv2.k8s.aws/pod-readiness-gate-inject=enabled, so a new "
                      "pod counts as ready before the ALB sends it traffic")

        webhook = ""
        for policy in self.kube("get", "mutatingwebhookconfigurations", "-o", "name", stderr=DROP).out.split():
            hooks = self.kube("get", policy, "-o", r'jsonpath={range .webhooks[*]}{.name}={.failurePolicy}{"\n"}{end}',
                              stderr=DROP).out
            webhook = "\n".join((line.split("=") + [""])[1] for line in hooks.splitlines()
                                if line.split("=")[0] == "mpod.elbv2.k8s.aws")
            if webhook:
                break
        if not webhook:
            self.warn("The Load Balancer Controller's pod webhook is not installed; run "
                      "ansible/playbooks/load-balancer-controller.yml")
        elif webhook == "Ignore":
            self.ok("Pod webhook failure policy is Ignore, so pods still start if the controller is down")
        else:
            self.warn(f"Pod webhook failure policy is {webhook}: pods cannot be created in labelled namespaces while "
                      "the controller is down")

        self.sub("Pods")
        pods = self.kube("get", "pods", "-n", self.namespace, "--field-selector=status.phase=Running",
                         "-o", "jsonpath={.items[*].metadata.name}", stderr=DROP).out
        if not pods:
            self.warn(f"No running pods in {self.namespace} to check")
        for pod in pods.split():
            gates = self.kube("get", "pod", pod, "-n", self.namespace,
                              "-o", "jsonpath={.spec.readinessGates[*].conditionType}", stderr=DROP).out
            if not gates:
                self.warn(f"{pod} has no readiness gate; it was created before the namespace label or the Ingress "
                          "existed. The next rollout fixes it")
                continue
            for gate in gates.split():
                status = self.kube("get", "pod", pod, "-n", self.namespace,
                                   "-o", f'jsonpath={{.status.conditions[?(@.type=="{gate}")].status}}',
                                   stderr=DROP).out
                name = gate.rsplit("/", 1)[-1]
                if status == "True":
                    self.ok(f"{pod} passes its ALB target check ({name})")
                else:
                    self.warn(f"{pod} gate {name} is {status or 'missing'}; the ALB has not called this pod healthy")

        self.sub("Drain time")
        annotation = self.kube("get", "ingress", self.ingress, "-n", self.namespace, "-o",
                               r"jsonpath={.metadata.annotations.alb\.ingress\.kubernetes\.io/target-group-attributes}",
                               stderr=DROP).out
        if "deregistration_delay.timeout_seconds" in annotation:
            self.ok(f"Ingress {self.ingress} sets {annotation}")
        else:
            self.warn(f"Ingress {self.ingress} does not set deregistration_delay.timeout_seconds, so target groups "
                      "drain for the default 300s")

        alb = self.aws("elbv2", "describe-load-balancers", "--names", self.alb_name,
                       "--query", "LoadBalancers[0].LoadBalancerArn", "--output", "text", stderr=DROP).out
        groups = self.aws("elbv2", "describe-target-groups", "--load-balancer-arn", alb,
                          "--query", "TargetGroups[].TargetGroupArn", "--output", "text", stderr=DROP).out
        if not groups or groups == "None":
            self.skip(f"No target groups found for {self.alb_name}")
            return
        for group in groups.split():
            delay = self.aws("elbv2", "describe-target-group-attributes", "--target-group-arn", group, "--query",
                             "Attributes[?Key=='deregistration_delay.timeout_seconds'].Value", "--output", "text",
                             stderr=DROP).out or "unknown"
            name = group.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[0-9]+", delay):
                self.warn(f"Could not read the drain time of {name}")
            elif int(delay) <= 60:
                self.ok(f"Target group {name} drains for {delay}s")
            else:
                self.warn(f"Target group {name} drains for {delay}s; a stopped pod stays registered that long")

    def section_capacity(self):
        self.section("Room for the extra pod")
        if not self.ensure_kube():
            return
        nodes = self.kube("get", "nodes", "--no-headers", "-o",
                          "custom-columns=NAME:.metadata.name,PODS:.status.allocatable.pods", stderr=DROP).out
        if not nodes:
            self.fail("Could not list nodes")
            return
        placed = self.kube("get", "pods", "-A", "--field-selector=status.phase=Running", "--no-headers",
                           "-o", "custom-columns=NODE:.spec.nodeName", stderr=CAPTURE)
        if not placed.ok:
            self.fail(f"Could not list the running pods, so the free pod slots are unknown: {last_line(placed.err)}")
            return
        pods_per_node = Counter(line.split()[0] for line in placed.out.splitlines() if line.split())

        total_free = 0
        for line in nodes.split("\n"):
            node, allocatable = fields(line, 2, separator=None)
            used = pods_per_node[node]
            free = (int(allocatable) if allocatable.isdigit() else 0) - used
            total_free += free
            print(f"{node:<40} {used}/{allocatable} pod slots used, {free} free")

            # "Allocated resources" prints the requests percentage first, then limits.
            described = self.kube("describe", "node", node, stderr=DROP)
            if not described.ok:
                self.warn(f"Could not read the resource requests on {node}")
            inside = False
            for row in described.out.splitlines():
                if row.startswith("Allocated resources:"):
                    inside = True
                words = row.split()
                if inside and words and words[0] in ("cpu", "memory"):
                    resource = words[0]
                    percent = re.sub(r"[()%]", "", words[2] if len(words) > 2 else "")
                    print(f"  {resource:<8} {percent}% requested")
                    if re.fullmatch(r"[0-9]+", percent) and int(percent) > 85:
                        self.warn(f"{node} has {percent}% of its {resource} requested; the extra pod a rollout starts "
                                  "may not fit")

        # One rollout adds one backend pod and one frontend pod.
        if total_free >= 2:
            self.ok(f"{total_free} free pod slots; a rollout needs 2 (one per Deployment)")
        else:
            self.fail(f"Only {total_free} free pod slot(s); a rollout needs 2 and the extra pods will stay Pending")

    def watch_rollout(self, deadline):
        """Sample both Deployments and probe the app until the rollout finishes."""
        started_at = now()
        samples = requests = failures = zero_ready = surge = unreadable = unreadable_in_a_row = 0
        first_failure = first_zero = first_unreadable = ""
        scaled_down = []
        print(f"\n{'TIME':<10} {'PODS (have/ready of wanted)':<42} {'FRONTEND':<22} BACKEND")

        while True:
            rolling = sample_surge = sample_zero = sample_unreadable = False
            state = ""
            for line in self.deployment_state()[0].split("\n"):
                deployment, generation, observed, spec, pods, updated, ready, _ = fields(line, 8, "|")
                short = deployment.rsplit("-", 1)[-1]
                if generation == "unreadable":
                    # Its ready pods are unknown and it may still be rolling.
                    rolling = sample_unreadable = True
                    first_unreadable = first_unreadable or first_read_error(line)
                    state += f"{short} ?  "
                    continue
                pods, updated, ready = pods or "0", updated or "0", ready or "0"
                state += f"{short} {pods}/{ready} of {spec}  "
                if spec == "0":
                    if deployment not in scaled_down:
                        scaled_down.append(deployment)
                    continue
                wanted = int(spec)
                if (observed or "0") != generation or int(updated) < wanted or int(pods) > wanted \
                        or int(ready) < wanted:
                    rolling = True
                if int(pods) > wanted:
                    sample_surge = True
                if int(ready) == 0:
                    sample_zero = True
                    first_zero = first_zero or deployment

            surge += sample_surge
            zero_ready += sample_zero
            if sample_unreadable:
                unreadable += 1
                unreadable_in_a_row += 1
            else:
                unreadable_in_a_row = 0

            frontend_code = self.probe(self.frontend_url).partition("|")[0]
            backend_code, _, backend_type = self.probe(self.backend_url).partition("|")
            requests += 2
            samples += 1
            # The frontend must answer 200. /api/health is not a backend route, so
            # any answer from the app below 500 means the request reached the backend.
            if frontend_code != "200":
                failures += 1
                first_failure = first_failure or f"{self.frontend_url} returned {frontend_code}"
            code = backend_code or "500"
            if backend_code == "000" or (re.fullmatch(r"[0-9]+", code) and int(code) >= 500):
                failures += 1
                first_failure = first_failure or f"{self.backend_url} returned {backend_code}"

            print(f"{utc_now('%H:%M:%S'):<10} {state:<42} {frontend_code:<22} {backend_code} {backend_type}")

            if not rolling and samples > 1:
                break
            if unreadable_in_a_row >= 3:
                self.fail(f"Stopped watching: the Deployments could not be read in {unreadable_in_a_row} samples "
                          "in a row")
                break
            if now() >= deadline:
                self.fail(f"The rollout did not finish within {self.rollout_timeout}s")
                break
            time.sleep(self.interval)

        sampled = f"{samples} samples over {now() - started_at}s, {requests} requests"
        if not failures:
            self.ok(f"No request failed during the rollout ({sampled})")
        else:
            self.fail(f"{failures} of {requests} requests failed during the rollout; first: {first_failure} ({sampled})")
        if zero_ready:
            self.fail(f"{first_zero} had 0 ready pods in {zero_ready} sample(s); the rollout is not keeping the app up")
        if unreadable:
            self.fail(f"Could not read the Deployments in {unreadable} of {samples} sample(s), so their ready pods "
                      f"are unconfirmed; first error: {first_unreadable}")
        if scaled_down:
            self.warn(f"Scaled to 0 replicas:{''.join(' ' + name for name in scaled_down)}; there were no pods "
                      "to keep ready")
        if not zero_ready and not unreadable and not scaled_down:
            self.ok("Both Deployments kept at least one ready pod the whole time")
        if surge:
            self.ok(f"Saw the extra pod start before the old one stopped in {surge} sample(s), which is what "
                    "RollingUpdate should do")
        else:
            self.warn("Never saw more pods than replicas; the rollout may have been too fast to sample, or no "
                      "rollout happened")

    def section_live(self):
        self.section("Live rollout")
        if not self.ensure_kube():
            return
        if not has_command("curl"):
            self.fail("curl is needed to probe the app during a rollout")
            return
        deadline = now() + self.rollout_timeout

        if self.mode == "restart":
            self.note("Restarting both Deployments; this replaces the running pods with the same image.")
            for deployment in self.deployments:
                restart = self.kube("rollout", "restart", f"deployment/{deployment}", "-n", self.namespace)
                if not restart.ok:
                    print_tail(restart.out)
                    self.fail(f"Could not restart {deployment}")
                    return
                self.ok(f"Restarted {deployment}")
        else:
            self.note(f"Waiting up to {self.rollout_timeout}s for a rollout to start. Trigger the deploy workflow "
                      "now, or run ansible/playbooks/deploy-image.yml.")
            before, readable = self.deployment_state()
            if not readable:
                self.fail(f"Could not read the Deployments: {first_read_error(before)}")
                return
            waiting_since = now()
            unreadable_in_a_row = 0
            while True:
                # A failed read is not a change; only a readable new state starts the watch.
                after, readable = self.deployment_state()
                if readable:
                    unreadable_in_a_row = 0
                    if after != before:
                        self.ok(f"A rollout started after {now() - waiting_since}s")
                        break
                else:
                    unreadable_in_a_row += 1
                    if unreadable_in_a_row >= 3:
                        self.fail(f"Stopped waiting for a rollout: the Deployments could not be read "
                                  f"{unreadable_in_a_row} times in a row; last error: {first_read_error(after)}")
                        return
                if now() >= deadline:
                    self.skip(f"No rollout started within {self.rollout_timeout}s")
                    return
                time.sleep(self.interval)

        self.watch_rollout(deadline)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", dest="mode", action="store_const", const="watch", default="settings",
                        help="wait for a rollout to start (a CI deploy, or someone else's kubectl), then probe the "
                             "app until it finishes")
    parser.add_argument("--restart", dest="mode", action="store_const", const="restart",
                        help="start a rollout with kubectl rollout restart and probe it; this restarts the running "
                             "pods with the same image")
    parser.add_argument("--interval", metavar="SECONDS", help="seconds between probes during a rollout (default 1)")
    parser.add_argument("--timeout", metavar="SECONDS", help="seconds to wait for the rollout (default 600)")
    args = parser.parse_args()

    settings = {
        "AWS_REGION": setting("AWS_REGION", "aws_region"),
        "NAMESPACE": setting("NAMESPACE", "k8s_namespace"),
        "BACKEND_DEPLOYMENT": setting("BACKEND_DEPLOYMENT", "backend_deployment"),
        "FRONTEND_DEPLOYMENT": setting("FRONTEND_DEPLOYMENT", "frontend_deployment"),
        "INGRESS_NAME": setting("INGRESS_NAME", "ingress_name"),
        "ALB_NAME": setting("ALB_NAME", "alb_name"),
        "APP_DOMAIN": setting("APP_DOMAIN", "app_domain_name"),
    }
    for variable, value in settings.items():
        if not value:
            raise ScriptError(f"Missing {variable}: export it, or run ./scripts/tf.py apply to generate "
                              f"{group_vars_dir()}/terraform.yml")

    interval = args.interval if args.interval is not None else setting("PROBE_INTERVAL", default="1")
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", interval) or float(interval) <= 0:
        raise ScriptError(f"PROBE_INTERVAL must be a positive number, got: {interval}")
    probe_timeout = setting("PROBE_TIMEOUT", default="3")
    rollout_timeout = args.timeout if args.timeout is not None else setting("ROLLOUT_TIMEOUT", default="600")
    for variable, value in (("PROBE_TIMEOUT", probe_timeout), ("ROLLOUT_TIMEOUT", rollout_timeout)):
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise ScriptError(f"{variable} must be a positive whole number, got: {value}")

    os.environ["AWS_PAGER"] = ""
    # The playbooks write the tunnel kubeconfig here and leave ~/.kube/config alone.
    os.environ["KUBECONFIG"] = str(REPO_ROOT / ".generated/kubeconfig")
    check = RolloutCheck(settings, args.mode, float(interval), probe_timeout, int(rollout_timeout))

    require_command("aws")
    require_command("curl")
    identity = run(["aws", "sts", "get-caller-identity", "--query", "[Account, Arn]", "--output", "text"],
                   stderr=DROP)
    if not identity.ok:
        raise ScriptError("No usable AWS credentials. Run: aws login")

    check.section("HospitalSystem rollout check")
    for name, value in (("Time", utc_now("%Y-%m-%d %H:%M:%S UTC")), ("Account", identity.out.partition("\t")[0]),
                        ("Region", check.region), ("Namespace", check.namespace), ("App", check.app_url),
                        ("Mode", args.mode)):
        print(f"{name:<10} {value}")

    sections = [("settings", check.section_settings), ("gates", check.section_gates),
                ("capacity", check.section_capacity)]
    if args.mode != "settings":
        sections.append(("live", check.section_live))
    return 0 if check.run_sections(sections) else 1


if __name__ == "__main__":
    entrypoint(main)
