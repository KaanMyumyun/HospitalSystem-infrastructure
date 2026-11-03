#!/usr/bin/env python3
"""Delete what would stall terraform destroy: the ALB and what the Load
Balancer Controller made for it.

In order: the Ingress, so the controller deletes its ALB; the ALB itself if
it's still there; the controller's k8s-* target groups and security groups;
detached network interfaces; and the ALB alarms. It also says whether the ACM
certificate is still in use.

Exits non-zero when a lookup fails or something can't be deleted, so a failed
cleanup is never followed by a destroy that stalls.

Environment overrides: AWS_REGION, VPC_ID, ALB_NAME, INGRESS_NAME,
K8S_NAMESPACE, CERT_ARN, ALARM_PREFIX, RETRY_DELAY.
"""

import argparse
import os
import sys
import time

from lib.commands import CAPTURE, DROP, SHOW, ScriptError, entrypoint, has_command, require_command, run
from lib.config import REPO_ROOT, alb_alarm_query, group_vars_dir, setting


def section(title):
    print(f"\n== {title} ==")


class Cleanup:
    def __init__(self, apply):
        self.apply = apply
        self.region = setting("AWS_REGION", "aws_region", "eu-north-1")
        self.vpc = setting("VPC_ID", "vpc_id")
        self.alb_name = setting("ALB_NAME", "alb_name", "hospital-system-alb")
        self.ingress = setting("INGRESS_NAME", "ingress_name", "hospital-ingress")
        self.namespace = setting("K8S_NAMESPACE", "k8s_namespace", "hospitalsystem")
        self.cert_arn = setting("CERT_ARN", "acm_certificate_arn")
        self.alarm_prefix = setting("ALARM_PREFIX", "monitoring_alarm_prefix", "hospitalsystem")
        self.alarm_query = alb_alarm_query(self.alarm_prefix)
        # Seconds between retries of an ALB wait or a delete that AWS reports as in use.
        delay = setting("RETRY_DELAY", default="10")
        try:
            self.retry_delay = float(delay)
        except ValueError:
            raise ScriptError(f"RETRY_DELAY must be a number of seconds, got: {delay}") from None
        self.found = self.deleted = self.failures = 0

    def lookup(self, absent, *args):
        """What an AWS lookup returns ("None" counts as nothing).

        The error code absent means the resource doesn't exist, which also
        gives ""; absent=None accepts no error. Any other error stops the
        script: an API error must never read as "nothing there".
        """
        result = run(["aws", *args], stderr=CAPTURE)
        if result.ok:
            return "" if result.out == "None" else result.out
        if absent and f"({absent})" in result.err:
            return ""
        raise ScriptError(f"aws {args[0]} {args[1]} failed: {result.err}")

    def delete_with_retry(self, in_use, gone, *args):
        """Run an AWS delete, retrying while AWS reports the resource in use.

        One that is already gone counts as deleted. Prints the last error and
        returns False when it gives up.
        """
        for attempt in range(1, 13):
            result = run(["aws", *args], stderr=CAPTURE)
            if result.ok or f"({gone})" in result.err:
                return True
            if f"({in_use})" not in result.err or attempt == 12:
                break
            time.sleep(self.retry_delay)
        print(f"could not delete {args[-1]}: {result.err}", file=sys.stderr)
        return False

    def alb_arn(self):
        return self.lookup("LoadBalancerNotFound", "elbv2", "describe-load-balancers", "--region", self.region,
                           "--names", self.alb_name, "--query", "LoadBalancers[0].LoadBalancerArn", "--output", "text")

    def wait_for_alb_gone(self, attempts):
        for _ in range(attempts):
            if not self.alb_arn():
                return True
            time.sleep(self.retry_delay)
        return False

    def ingress_section(self):
        """Delete the Ingress, so the controller deletes the ALB; True when it did."""
        section("Kubernetes Ingress")
        if not has_command("kubectl"):
            print("kubectl not installed - skipping, will delete the ALB directly")
            return False
        if not run(["kubectl", "get", "--raw", "/readyz", "--request-timeout=15s"], stderr=DROP).ok:
            print("Cluster unreachable - skipping, will delete the ALB directly")
            print("To let the controller delete it instead, open the tunnel first with")
            print("ansible-playbook ansible/playbooks/kubeconfig.yml")
            print("If a tunnel from before a cluster rebuild is still open, close it with")
            print("pkill -f AWS-StartPortForwardingSessionToRemoteHost")
            return False
        if not run(["kubectl", "get", "ingress", self.ingress, "-n", self.namespace, "--request-timeout=15s"],
                   stderr=DROP).ok:
            print(f"Ingress {self.namespace}/{self.ingress} not present")
            return False
        self.found += 1
        print(f"ingress {self.namespace}/{self.ingress}")
        if not self.apply:
            return False
        run(["kubectl", "delete", "ingress", self.ingress, "-n", self.namespace, "--ignore-not-found=true",
             "--wait=false", "--request-timeout=20s"], capture=False, check=True)
        self.deleted += 1
        print("Waiting for the controller to delete the ALB...")
        if not self.wait_for_alb_gone(18):
            print("The controller did not delete it in time")
        return True

    def load_balancer_section(self, ingress_deleted):
        section("Load balancer")
        arn = self.alb_arn()
        if not arn:
            print("gone (deleted by the controller)" if ingress_deleted else "none")
            return
        self.found += 1
        print(arn)
        if not self.apply:
            return
        print("Controller did not remove it - deleting directly")
        run(["aws", "elbv2", "delete-load-balancer", "--region", self.region, "--load-balancer-arn", arn],
            capture=False, check=True)
        print("Waiting for the ALB to disappear...")
        if self.wait_for_alb_gone(30):
            self.deleted += 1
        else:
            print("the ALB is still there", file=sys.stderr)
            self.failures += 1

    def tally(self, deleted):
        if deleted:
            self.deleted += 1
        else:
            self.failures += 1

    def target_groups_section(self):
        section("Orphaned ALB target groups")
        arns = self.lookup(None, "elbv2", "describe-target-groups", "--region", self.region, "--query",
                           f"TargetGroups[?starts_with(TargetGroupName, `k8s-`) && VpcId=='{self.vpc}' && "
                           "length(LoadBalancerArns) == `0`].TargetGroupArn", "--output", "text").split()
        if not arns:
            print("none")
        for arn in arns:
            self.found += 1
            print(arn)
            # Right after the ALB goes, its target groups can still read as in use.
            if self.apply:
                self.tally(self.delete_with_retry("ResourceInUse", "TargetGroupNotFound", "elbv2",
                                                  "delete-target-group", "--region", self.region,
                                                  "--target-group-arn", arn))

    def security_groups_section(self):
        section("Kubernetes-managed security groups")
        groups = self.lookup(None, "ec2", "describe-security-groups", "--region", self.region, "--filters",
                             f"Name=vpc-id,Values={self.vpc}", "Name=group-name,Values=k8s-*",
                             "--query", "SecurityGroups[].GroupId", "--output", "text").split()
        if not groups:
            print("none")
            return
        for group in groups:
            self.found += 1
            print(group)
        if not self.apply:
            return
        # A rule in another group that refers to one of these blocks its delete.
        for group in groups:
            referencing = self.lookup(None, "ec2", "describe-security-groups", "--region", self.region, "--filters",
                                      f"Name=vpc-id,Values={self.vpc}", f"Name=ip-permission.group-id,Values={group}",
                                      "--query", "SecurityGroups[].GroupId", "--output", "text").split()
            for other in referencing:
                rules = self.lookup(None, "ec2", "describe-security-group-rules", "--region", self.region,
                                    "--filters", f"Name=group-id,Values={other}", "--query",
                                    f"SecurityGroupRules[?!IsEgress && ReferencedGroupInfo.GroupId=='{group}']"
                                    ".SecurityGroupRuleId", "--output", "text")
                if rules:
                    run(["aws", "ec2", "revoke-security-group-ingress", "--region", self.region, "--group-id", other,
                         "--security-group-rule-ids", *rules.split()], stderr=SHOW, check=True)
                    print(f"revoked {rules} in {other}")
        # ALB network interfaces can hold a group for a while after the ALB goes.
        for group in groups:
            if self.delete_with_retry("DependencyViolation", "InvalidGroup.NotFound", "ec2", "delete-security-group",
                                      "--region", self.region, "--group-id", group):
                print(f"deleted {group}")
                self.deleted += 1
            else:
                self.failures += 1

    def network_interfaces_section(self):
        section("Detached network interfaces")
        interfaces = self.lookup(None, "ec2", "describe-network-interfaces", "--region", self.region, "--filters",
                                 f"Name=vpc-id,Values={self.vpc}", "Name=status,Values=available",
                                 "--query", "NetworkInterfaces[].NetworkInterfaceId", "--output", "text").split()
        if not interfaces:
            print("none")
        for interface in interfaces:
            self.found += 1
            print(interface)
            if self.apply:
                self.tally(self.delete_with_retry("InvalidNetworkInterface.InUse", "InvalidNetworkInterfaceID.NotFound",
                                                  "ec2", "delete-network-interface", "--region", self.region,
                                                  "--network-interface-id", interface))

    def alarms_section(self):
        section("ALB alarms (not blocking)")
        # ansible/playbooks/monitoring.yml creates these from
        # config/alb-alarms.json; Terraform doesn't know them. The node group
        # alarm is Terraform's and goes with the destroy. These don't block the
        # destroy, but nothing else would delete them, so a failed lookup stops
        # here too.
        alarms = self.lookup(None, "cloudwatch", "describe-alarms", "--region", self.region, "--alarm-name-prefix",
                             f"{self.alarm_prefix}-", "--query", self.alarm_query, "--output", "text").split()
        if not alarms:
            print("none")
            return
        for name in alarms:
            print(name)
        if self.apply:
            run(["aws", "cloudwatch", "delete-alarms", "--region", self.region, "--alarm-names", *alarms],
                capture=False, check=True)
            print("deleted")

    def certificate_section(self):
        section("ACM certificate (informational)")
        if not self.cert_arn:
            print("no certificate arn in group_vars - skipping")
            return
        # JSON keeps a certificate that is gone (prints nothing) apart from one
        # that nothing uses ([]).
        in_use = self.lookup("ResourceNotFoundException", "acm", "describe-certificate", "--region", self.region,
                             "--certificate-arn", self.cert_arn, "--query", "Certificate.InUseBy", "--output", "json")
        if not in_use:
            print("not found - already deleted")
        elif in_use in ("[]", "null"):
            print("not in use - terraform destroy can delete it")
        else:
            print(f"still in use by:\n{in_use}")
            print("\nTerraform will fail to delete the certificate while this holds it.")

    def run(self):
        require_command("aws")
        if not run(["aws", "sts", "get-caller-identity"], stderr=DROP).ok:
            raise ScriptError("No usable AWS credentials. Run: aws login")
        if not self.vpc:
            raise ScriptError(f"Could not determine the VPC id from {group_vars_dir()}.\n"
                              f"Pass it explicitly: VPC_ID=vpc-xxxx {sys.argv[0]} --apply")

        print(f"Region:    {self.region}\nVPC:       {self.vpc}\nALB:       {self.alb_name}\n"
              f"Ingress:   {self.namespace}/{self.ingress}")
        if not self.apply:
            print("\nDry run - nothing will be deleted. Re-run with --apply.")

        self.load_balancer_section(self.ingress_section())
        self.target_groups_section()
        self.security_groups_section()
        self.network_interfaces_section()
        self.alarms_section()
        self.certificate_section()

        section("Summary")
        if not self.apply:
            print(f"Found {self.found} blocking resources. Re-run with --apply to delete them.")
            return 0
        print(f"Deleted {self.deleted} of {self.found} blocking resources.")
        if self.failures:
            print(f"{self.failures} could not be deleted (see above). Fix that and run this again before "
                  "./scripts/tf.sh destroy.", file=sys.stderr)
            return 1
        print("Now run: ./scripts/tf.sh destroy")
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="delete what is found; without it the script only lists")
    apply = parser.parse_args().apply
    # The playbooks write the tunnel kubeconfig here and leave ~/.kube/config alone.
    os.environ["KUBECONFIG"] = str(REPO_ROOT / ".generated/kubeconfig")
    return Cleanup(apply).run()


if __name__ == "__main__":
    entrypoint(main)
