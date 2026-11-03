#!/usr/bin/env python3
"""Read operational settings with the same file precedence as Ansible.

Only literal scalar values are exported to Bash. Structured values and Jinja
expressions belong to Ansible and are never evaluated as shell code.
"""

import re
import sys
from pathlib import Path

import yaml


def read_config(directory):
    settings = {}
    for name in ("main.yml", "terraform.yml"):
        path = Path(directory) / name
        if not path.exists():
            continue
        values = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            raise ValueError(f"{path}: expected a YAML mapping")
        settings.update(values)
    return settings


def shell_values(settings):
    for key, value in settings.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key):
            raise ValueError("configuration keys must be identifiers")
        if value is None or isinstance(value, (list, dict)):
            continue
        if isinstance(value, bool):
            value = str(value).lower()
        else:
            value = str(value)
        if "{{" in value or "{%" in value:
            continue
        if any(char in value for char in "\r\n\0"):
            raise ValueError(f"{key}: operational settings must be single-line values")
        yield f"{key}={value}"


def main():
    try:
        # Validate everything before printing a partial configuration.
        values = list(shell_values(read_config(sys.argv[1])))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Could not load operational configuration: {exc}", file=sys.stderr)
        return 1
    print("\n".join(values))
    return 0


if __name__ == "__main__":
    sys.exit(main())
