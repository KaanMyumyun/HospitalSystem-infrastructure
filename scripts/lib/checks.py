"""What image-check.py, rollout-check.py and monitoring.py share: OK, WARN,
FAIL and SKIP lines, the summary, and the aws, kubectl and ansible-playbook
calls they make.
"""

from datetime import datetime, timezone
import os
import sys

from lib.commands import run
from lib.config import REPO_ROOT


class Report:
    """Prints results as OK, WARN, FAIL and SKIP lines and counts them."""

    def __init__(self, name_width=14):
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        codes = ("\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m")
        self.bold, self.dim, self.green, self.yellow, self.red, self.reset = codes if color else ("",) * 6
        # Width of the summary's section column.
        self.name_width = name_width
        # (status, section, message) for each result, in order.
        self.results = []
        # The section results are listed under in the summary.
        self.current = ""

    def section(self, title):
        print(f"\n{self.bold}== {title} =={self.reset}")

    def sub(self, title):
        print(f"\n{self.bold}-- {title} --{self.reset}")

    def note(self, text):
        print(f"{self.dim}{text}{self.reset}")

    def result(self, status, color, message):
        self.results.append((status, self.current, message))
        print(f"{color}{status:<4}{self.reset} {message}")

    def ok(self, message):
        self.result("OK", self.green, message)

    def warn(self, message):
        self.result("WARN", self.yellow, message)

    def fail(self, message):
        self.result("FAIL", self.red, message)

    def skip(self, message):
        self.result("SKIP", self.dim, message)

    def relay(self, text):
        """Print text, recording its lines that start with WARN<tab> as warnings."""
        for line in text.splitlines():
            if line.startswith("WARN\t"):
                self.warn(line.split("\t", 1)[1])
            else:
                print(line)

    def count(self, status):
        return sum(1 for result in self.results if result[0] == status)

    def summary(self):
        """Print the counts and every result that isn't OK; True when nothing failed."""
        self.section("Summary")
        print(f"{self.green}{self.count('OK')} ok{self.reset}, "
              f"{self.yellow}{self.count('WARN')} warning(s){self.reset}, "
              f"{self.red}{self.count('FAIL')} failure(s){self.reset}, {self.count('SKIP')} skipped")
        colors = {"FAIL": self.red, "WARN": self.yellow}
        for status, name, message in self.results:
            if status != "OK":
                print(f"{colors.get(status, self.dim)}{status:<4}{self.reset} "
                      f"{name:<{self.name_width}} {message}")
        return self.count("FAIL") == 0


class Checks(Report):
    """A Report that also runs the aws, kubectl and ansible-playbook calls."""

    def __init__(self, region, kube_timeout="30s", name_width=14):
        super().__init__(name_width)
        self.region = region
        self.kube_timeout = kube_timeout

    def aws(self, *args, **options):
        return run(["aws", "--region", self.region, *args], **options)

    def kube(self, *args, **options):
        return run(["kubectl", f"--request-timeout={self.kube_timeout}", *args], **options)

    def ansible(self, *args, **options):
        return run(["ansible-playbook", *args], cwd=REPO_ROOT, **options)


def parse_time(text):
    """text as an aware datetime (UTC when it has no offset), or None when it
    isn't an ISO 8601 timestamp."""
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
