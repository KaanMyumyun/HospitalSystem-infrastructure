"""The shared code in scripts/lib: running commands, Ctrl+C, and the report."""

import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest import TestCase, main

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.checks import Report
from lib.commands import DROP, CommandFailed, fields, last_line, run

# A script whose main() runs BODY and then prints "after".
SCRIPT = """
import os
import sys
import time

sys.path.insert(0, SCRIPTS)
from lib.commands import entrypoint, run


def main():
    BODY
    print("after")
    return 0


entrypoint(main)
"""


class RunTests(TestCase):
    def test_output_loses_trailing_newlines_and_stderr_goes_where_asked(self):
        command = ["sh", "-c", "echo out; echo err >&2; printf '\\n\\n'"]
        self.assertEqual(run(command).out, "out\nerr")
        self.assertEqual(run(command, stderr=DROP).out, "out")
        result = run(["sh", "-c", "exit 3"])
        self.assertEqual((result.code, result.ok), (3, False))

    def test_a_missing_command_exits_127_like_in_a_shell(self):
        result = run(["no-such-command-for-this-test"])
        self.assertEqual(result.code, 127)
        self.assertIn("command not found", result.out)

    def test_a_failure_with_check_stops_the_script_with_the_commands_code(self):
        with self.assertRaises(CommandFailed) as raised:
            run(["sh", "-c", "exit 4"], check=True)
        self.assertEqual(raised.exception.code, 4)

    def test_fields_keep_an_empty_field_in_its_place(self):
        # read with a tab IFS would shift c0ffee1 into the empty field.
        self.assertEqual(fields("ahead\t3\t0\t0\t\tc0ffee1\nnext line", 6), ["ahead", "3", "0", "0", "", "c0ffee1"])
        self.assertEqual(fields("a\tb\tc", 2), ["a", "b\tc"])
        self.assertEqual(fields("a", 3), ["a", "", ""])
        self.assertEqual(fields("  PendingDeletion\tCUSTOMER  2026-10-01 x  ", 3, separator=None),
                         ["PendingDeletion", "CUSTOMER", "2026-10-01 x"])

    def test_last_line_skips_blank_lines(self):
        self.assertEqual(last_line("first\nAccessDenied: no\n  \n"), "AccessDenied: no")
        self.assertEqual(last_line(""), "")


class InterruptTests(TestCase):
    """Ctrl+C reaches the script and the command it runs, as in a terminal."""

    def interrupt(self, body):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        program = tmp / "script.py"
        program.write_text(SCRIPT.replace("SCRIPTS", repr(str(ROOT / "scripts"))).replace("BODY", body))
        # Its own process group, so the signal reaches the script and its
        # command together, like Ctrl+C does.
        process = subprocess.Popen([sys.executable, str(program)], env={**os.environ, "DIR": str(tmp)},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        deadline = time.monotonic() + 10
        while not (tmp / "started").exists():
            self.assertLess(time.monotonic(), deadline, "the script never started")
            time.sleep(0.05)
        os.killpg(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=20)
        return process.returncode, stdout, stderr, tmp

    def test_the_script_waits_for_the_command_to_stop_then_exits_130(self):
        # Like terraform, the command takes a while to stop after Ctrl+C.
        child = ('trap \'sleep 1; touch "$DIR/finished"; exit 1\' INT; '
                 'touch "$DIR/started"; while true; do sleep 0.1; done')
        code, stdout, stderr, tmp = self.interrupt(f"run(['bash', '-c', {child!r}], capture=False)")
        self.assertEqual(code, 130, stderr)
        self.assertTrue((tmp / "finished").exists(), "the command was killed before it finished")
        self.assertNotIn("after", stdout)
        self.assertNotIn("Traceback", stderr)

    def test_ctrl_c_between_commands_stops_at_once(self):
        code, stdout, stderr, _ = self.interrupt('open(os.environ["DIR"] + "/started", "w").close(); time.sleep(30)')
        self.assertEqual(code, 130, stderr)
        self.assertNotIn("after", stdout)
        self.assertNotIn("Traceback", stderr)


class ReportTests(TestCase):
    def test_results_then_a_summary_of_what_is_not_ok(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report = Report(name_width=8)
            report.current = "alarms"
            report.ok("fine")
            report.warn("odd")
            report.current = "logs"
            report.fail("broken")
            report.skip("not here")
            report.relay("plain\nWARN\tfrom a table")
            passed = report.summary()
        self.assertFalse(passed)
        self.assertEqual(output.getvalue(), (
            "OK   fine\nWARN odd\nFAIL broken\nSKIP not here\nplain\nWARN from a table\n"
            "\n== Summary ==\n1 ok, 2 warning(s), 1 failure(s), 1 skipped\n"
            "WARN alarms   odd\nFAIL logs     broken\nSKIP logs     not here\nWARN logs     from a table\n"
        ))


if __name__ == "__main__":
    main()
