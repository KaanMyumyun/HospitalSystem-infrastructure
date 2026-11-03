"""Running the command-line tools the scripts drive.

The scripts call aws, kubectl, terraform, ansible-playbook, gh and docker the
way you would type them, rather than through their libraries, so the tests can
put fake tools first on PATH.
"""

from dataclasses import dataclass
import shutil
import signal
import subprocess
import sys

# Where run() sends a command's stderr: into the result after stdout (2>&1),
# away (2>/dev/null), straight to the terminal, or into the result's err.
MERGE = subprocess.STDOUT
DROP = subprocess.DEVNULL
SHOW = None
CAPTURE = subprocess.PIPE


class ScriptError(Exception):
    """Stops the script: entrypoint() prints the message on stderr and exits 1."""


class CommandFailed(Exception):
    """A command the script can't go on without failed. It printed its own
    error, so entrypoint() just exits with its code, as set -e did."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass
class Result:
    code: int
    out: str
    err: str = ""

    @property
    def ok(self):
        return self.code == 0


# Commands running now, and whether Ctrl+C arrived while one was.
_running = 0
_interrupted = False


def _on_interrupt(signum, frame):
    global _interrupted
    if not _running:
        raise KeyboardInterrupt
    # The command got the same Ctrl+C from the terminal. Let it stop on its own
    # (terraform saves its state and releases the lock first), then stop.
    _interrupted = True


def run(args, *, stderr=MERGE, capture=True, check=False, input=None, cwd=None, env=None):
    """Run a command and return its exit code and output.

    The output is stdout (with stderr after it for MERGE) without trailing
    newlines, as $(...) gives it. capture=False leaves stdout on the terminal.
    check=True raises CommandFailed when the command fails.
    """
    global _running
    _running += 1
    try:
        completed = subprocess.run(
            args, input=input, cwd=cwd, env=env, stderr=stderr,
            stdout=subprocess.PIPE if capture else None, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        message = f"{args[0]}: command not found"
        if stderr is SHOW:
            print(message, file=sys.stderr)
        return Result(127, message if stderr == MERGE else "", message if stderr == CAPTURE else "")
    finally:
        _running -= 1
    if _interrupted:
        raise KeyboardInterrupt
    if check and completed.returncode != 0:
        raise CommandFailed(completed.returncode)
    return Result(completed.returncode, (completed.stdout or "").rstrip("\n"), (completed.stderr or "").rstrip("\n"))


def has_command(name):
    return shutil.which(name) is not None


def require_command(name):
    if not has_command(name):
        raise ScriptError(f"Missing required command: {name}")


def last_line(text):
    """The last line of text that isn't blank, for a one-line error message."""
    return next((line for line in reversed(text.splitlines()) if line.strip()), "")


def fields(text, count, separator="\t"):
    """Split the first line of text into count fields, padded with "".

    Like read -r with count names: the last field keeps any further
    separators. Unlike read with a tab IFS, an empty field keeps its place
    instead of shifting the next one into it. separator=None splits on runs
    of whitespace, like read with the default IFS.
    """
    line = text.split("\n", 1)[0]
    parts = line.split(separator, count - 1)
    if separator is None and parts:
        parts[-1] = parts[-1].rstrip()
    return parts + [""] * (count - len(parts))


def print_tail(text, lines=15):
    """Print the end of a failed command's output, indented."""
    for line in text.splitlines()[-lines:]:
        print(f"    {line}")


def entrypoint(main):
    """Run main() as the script and exit with the code it returns.

    Output goes out line by line even into a pipe, a ScriptError is printed on
    stderr with exit 1, and Ctrl+C exits 130 without a traceback.
    """
    sys.stdout.reconfigure(line_buffering=True)
    signal.signal(signal.SIGINT, _on_interrupt)
    # Exit quietly when the reader goes away (| head), as a shell script does.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        code = main()
    except ScriptError as error:
        print(error, file=sys.stderr)
        code = 1
    except CommandFailed as failure:
        code = failure.code
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
