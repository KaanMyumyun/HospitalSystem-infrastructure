"""The settings the scripts read from the repository.

group_var() reads ansible/group_vars/all the way Ansible does: main.yml, then
terraform.yml, which Terraform writes on apply, over it. Only plain values come
back; lists, maps and Jinja expressions belong to Ansible.
"""

import functools
import json
import os
from pathlib import Path
import re

from lib.commands import ScriptError

REPO_ROOT = Path(__file__).resolve().parents[2]


def group_vars_dir():
    return Path(os.environ.get("GROUP_VARS_DIR") or REPO_ROOT / "ansible/group_vars/all")


def read_group_vars(directory):
    """The plain settings in directory's main.yml and terraform.yml, as strings.

    Everything is checked before anything is returned, so a bad file never
    leaves a script running with half its settings.
    """
    # Imported here: tf.py reads no group_vars, so it doesn't need PyYAML.
    import yaml

    settings = {}
    try:
        for name in ("main.yml", "terraform.yml"):
            path = Path(directory) / name
            if not path.exists():
                continue
            values = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError(f"{path}: expected a YAML mapping")
            settings.update(values)
        return dict(_plain_values(settings))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ScriptError(f"Could not load operational configuration: {exc}") from exc


def _plain_values(settings):
    for key, value in settings.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key):
            raise ValueError("configuration keys must be identifiers")
        if value is None or isinstance(value, (list, dict)):
            continue
        value = str(value).lower() if isinstance(value, bool) else str(value)
        if "{{" in value or "{%" in value:
            continue
        if any(char in value for char in "\r\n\0"):
            raise ValueError(f"{key}: operational settings must be single-line values")
        yield key, value


@functools.cache
def _group_vars(directory):
    return read_group_vars(directory)


def group_var(name):
    """A group_vars setting; "" when it isn't set."""
    return _group_vars(group_vars_dir()).get(name, "")


def setting(variable, name=None, default=""):
    """$variable when it's set, else the group_vars setting name, else default.

    The environment overrides both group_vars files, like ${VAR:-...} did.
    """
    return os.environ.get(variable) or (group_var(name) if name else "") or default


def alb_alarm_query(prefix, path=None):
    """A describe-alarms --query that selects the ALB alarms for prefix.

    The alarms are the ones config/alb-alarms.json defines. monitoring.yml
    appends the target group to a target group alarm's name, so those match
    by prefix and the others exactly.
    """
    path = path or REPO_ROOT / "config/alb-alarms.json"
    alarms = json.loads(Path(path).read_text(encoding="utf-8"))
    filters = []
    for alarm in alarms:
        name = f"{prefix}-{alarm['suffix']}"
        if not re.fullmatch(r"[A-Za-z0-9-]+", name):
            raise ScriptError(f"{path}: alarm name {name!r} must be letters, digits and hyphens")
        if alarm["scope"] == "target_group":
            filters.append(f"starts_with(AlarmName, '{name}')")
        elif alarm["scope"] == "load_balancer":
            filters.append(f"AlarmName == '{name}'")
        else:
            raise ScriptError(f"{path}: unknown alarm scope {alarm['scope']!r}")
    if not filters:
        raise ScriptError(f"{path}: no alarms defined")
    return f"MetricAlarms[?{' || '.join(filters)}].AlarmName"
