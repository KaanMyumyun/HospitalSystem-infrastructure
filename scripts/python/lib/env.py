"""Load .env.local with the shell syntax tf.py and Ansible accept."""

import json
import subprocess
import sys

from lib.commands import ScriptError


def load_env(env_file):
    """Return the environment after sourcing env_file, with values exported."""
    script = (
        'set -euo pipefail; set -a; source "$1"; set +a; '
        'exec "$2" -c "import json, os, sys; json.dump(dict(os.environ), sys.stdout)"'
    )
    result = subprocess.run(["bash", "-c", script, "bash", str(env_file), sys.executable],
                            capture_output=True, text=True)
    if result.returncode:
        raise ScriptError(f"could not source {env_file}: {result.stderr.strip()}")
    return json.loads(result.stdout)
