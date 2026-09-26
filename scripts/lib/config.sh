#!/usr/bin/env bash
# Source after setting REPO_ROOT. Callers keep environment overrides with
# VAR="${VAR:-$(group_var setting)}"; generated values override main.yml.
GROUP_VARS_DIR="${GROUP_VARS_DIR:-$REPO_ROOT/ansible/group_vars/all}"
declare -A GROUP_CONFIG=()

load_group_vars() {
  local values key value
  values="$(python3 "$REPO_ROOT/scripts/read-config.py" "$GROUP_VARS_DIR")" || return 1
  while IFS='=' read -r key value; do
    [ -n "$key" ] || continue
    GROUP_CONFIG["$key"]="$value"
  done <<< "$values"
}

group_var() {
  printf '%s' "${GROUP_CONFIG[$1]-}"
}

# describe-alarms --query selecting the ALB alarms config/alb-alarms.json
# defines for PREFIX. monitoring.yml appends the target group to a target
# group alarm's name, so those match by prefix and the others exactly.
alb_alarm_query() {
  python3 - "$REPO_ROOT/config/alb-alarms.json" "$1" <<'PY'
import json
import re
import sys

path, prefix = sys.argv[1:]
with open(path, encoding="utf-8") as source:
    alarms = json.load(source)
filters = []
for alarm in alarms:
    name = f"{prefix}-{alarm['suffix']}"
    if not re.fullmatch(r"[A-Za-z0-9-]+", name):
        sys.exit(f"{path}: alarm name {name!r} must be letters, digits and hyphens")
    if alarm["scope"] == "target_group":
        filters.append(f"starts_with(AlarmName, '{name}')")
    elif alarm["scope"] == "load_balancer":
        filters.append(f"AlarmName == '{name}'")
    else:
        sys.exit(f"{path}: unknown alarm scope {alarm['scope']!r}")
if not filters:
    sys.exit(f"{path}: no alarms defined")
print(f"MetricAlarms[?{' || '.join(filters)}].AlarmName")
PY
}

load_group_vars
