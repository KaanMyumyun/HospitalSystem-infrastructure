#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GROUP_VARS_DIR="$REPO_ROOT/ansible/group_vars/all"

group_var() {
  sed -nE "s/^\"?$1\"?:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/p" \
    "$GROUP_VARS_DIR/main.yml" "$GROUP_VARS_DIR/terraform.yml" 2>/dev/null | tail -n 1 || true
}

tf_default() {
  sed -nE "/^variable \"$1\"/,/^}/ s/^[[:space:]]*default[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" \
    "$REPO_ROOT/terraform/variables.tf" 2>/dev/null || true
}

#./scripts/monitoring.sh                    # everything, including cost; changes nothing in AWS
#./scripts/monitoring.sh --refresh-alarms   # also recreates the ALB alarms (monitoring.yml) first
#./scripts/monitoring.sh workloads logs     # only the sections you name


AWS_REGION="${AWS_REGION:-$(group_var aws_region)}"
EKS_CLUSTER_NAME="${EKS_CLUSTER_NAME:-$(group_var eks_cluster_name)}"
NAMESPACE="${NAMESPACE:-$(group_var k8s_namespace)}"
BACKEND_DEPLOYMENT="${BACKEND_DEPLOYMENT:-$(group_var backend_deployment)}"
FRONTEND_DEPLOYMENT="${FRONTEND_DEPLOYMENT:-$(group_var frontend_deployment)}"
INGRESS_NAME="${INGRESS_NAME:-$(group_var ingress_name)}"
ALB_NAME="${ALB_NAME:-$(group_var alb_name)}"
ALARM_PREFIX="${ALARM_PREFIX:-$(group_var monitoring_alarm_prefix)}"
APP_DOMAIN="${APP_DOMAIN:-$(group_var app_domain_name)}"
ACM_CERT_ARN="${ACM_CERT_ARN:-$(group_var acm_certificate_arn)}"
OPS_INSTANCE_ID="${OPS_INSTANCE_ID:-$(group_var ops_instance_id)}"
BACKEND_REPOSITORY_URL="${BACKEND_REPOSITORY_URL:-$(group_var backend_repository_url)}"
FRONTEND_REPOSITORY_URL="${FRONTEND_REPOSITORY_URL:-$(group_var frontend_repository_url)}"
DEPLOY_GROUP="${DEPLOY_GROUP:-$(group_var github_actions_deploy_group)}"
# terraform/ops.tf names the deploy document after var.project_name, which is also the alarm prefix.
DEPLOY_SSM_DOCUMENT="${DEPLOY_SSM_DOCUMENT:-${ALARM_PREFIX:+$ALARM_PREFIX-deploy}}"
GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-$(tf_default github_repository)}"
GITHUB_DEPLOY_ENVIRONMENT="${GITHUB_DEPLOY_ENVIRONMENT:-$(tf_default github_deploy_environment)}"
BACKEND_METRICS_PORT="${BACKEND_METRICS_PORT:-9091}"
LOG_TAIL_LINES="${LOG_TAIL_LINES:-100}"
LOOKBACK_HOURS="${LOOKBACK_HOURS:-3}"
KUBE_TIMEOUT="${KUBE_TIMEOUT:-30s}"

ALL_SECTIONS=(endpoints alarms metrics alb eks nodes system workloads app events logs control-plane ecr cicd cost)
REFRESH_ALARMS=false
SELECTED=()

usage() {
  cat <<'USAGE'
Usage: scripts/monitoring.sh [--refresh-alarms] [section ...]

Runs every monitoring check for HospitalSystem and ends with a summary of
warnings and failures. Exits 1 when any check fails. With no sections it runs
all of them. Nothing in AWS is changed unless --refresh-alarms is given.

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
  ecr            newest images, scan findings, whether the newest image is deployed
  cicd           SSM deploy history, GitHub Actions runs, repository variables vs Terraform
  cost           month-to-date spend without credits (one Cost Explorer call, $0.01)

Options:
  --refresh-alarms  recreate the ALB alarms for the current target groups
                    (ansible/playbooks/monitoring.yml) before reading them
  -h, --help        show this help

Environment overrides: AWS_REGION, EKS_CLUSTER_NAME, NAMESPACE,
BACKEND_DEPLOYMENT, FRONTEND_DEPLOYMENT, INGRESS_NAME, ALB_NAME, ALARM_PREFIX,
APP_DOMAIN, ACM_CERT_ARN, OPS_INSTANCE_ID, DEPLOY_SSM_DOCUMENT,
GITHUB_REPOSITORY, BACKEND_METRICS_PORT (9091), LOG_TAIL_LINES (100),
LOOKBACK_HOURS (3), KUBE_TIMEOUT (30s), NO_COLOR.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --refresh-alarms) REFRESH_ALARMS=true ;;
    -h | --help) usage; exit 0 ;;
    -*) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 1 ;;
    *)
      if [[ " ${ALL_SECTIONS[*]} " != *" $1 "* ]]; then
        printf 'Unknown section: %s\n\n' "$1" >&2
        usage >&2
        exit 1
      fi
      SELECTED+=("$1")
      ;;
  esac
  shift
done

if [ ${#SELECTED[@]} -eq 0 ]; then
  SELECTED=("${ALL_SECTIONS[@]}")
fi

for var in AWS_REGION EKS_CLUSTER_NAME NAMESPACE BACKEND_DEPLOYMENT FRONTEND_DEPLOYMENT INGRESS_NAME ALB_NAME ALARM_PREFIX APP_DOMAIN; do
  if [ -z "${!var}" ]; then
    printf 'Missing %s: export it, or run ./scripts/tf.sh apply to generate %s\n' \
      "$var" "$GROUP_VARS_DIR/terraform.yml" >&2
    exit 1
  fi
done

for var in LOG_TAIL_LINES LOOKBACK_HOURS BACKEND_METRICS_PORT; do
  if ! [[ "${!var}" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s must be a positive whole number, got: %s\n' "$var" "${!var}" >&2
    exit 1
  fi
done

APP_URL="https://$APP_DOMAIN"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\e[1m' DIM=$'\e[2m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RED=$'\e[31m' RESET=$'\e[0m'
else
  BOLD="" DIM="" GREEN="" YELLOW="" RED="" RESET=""
fi

export AWS_PAGER=""
# The playbooks write the tunnel kubeconfig here and leave ~/.kube/config alone.
export KUBECONFIG="$REPO_ROOT/.generated/kubeconfig"
NOW="$(date -u +%s)"
WINDOW_START=$((NOW - LOOKBACK_HOURS * 3600))
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

RESULTS=()
CURRENT_SECTION=""
KUBE_STATE=""
ALB_LOOKED_UP=false
ALB_ARN=""
ALB_DNS=""
ALB_STATE=""
ALB_PROBLEM=""
TG_PROBLEM=""
TG_ARNS=()
declare -A TG_LABEL=()

section() {
  printf '\n%s== %s ==%s\n' "$BOLD" "$1" "$RESET"
}

sub() {
  printf '\n%s-- %s --%s\n' "$BOLD" "$1" "$RESET"
}

note() {
  printf '%s%s%s\n' "$DIM" "$1" "$RESET"
}

result() {
  RESULTS+=("$1|$CURRENT_SECTION|$3")
  printf '%s%-4s%s %s\n' "$2" "$1" "$RESET" "$3"
}

ok() { result OK "$GREEN" "$1"; }
warn() { result WARN "$YELLOW" "$1"; }
fail() { result FAIL "$RED" "$1"; }
skip() { result SKIP "$DIM" "$1"; }

# Prints awk/table output and records the lines tagged WARN<tab> as warnings.
relay() {
  local line
  while IFS= read -r line; do
    case "$line" in
      WARN$'\t'*) warn "${line#*$'\t'}" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done
}

has_command() {
  command -v "$1" >/dev/null 2>&1
}

require_command() {
  if ! has_command "$1"; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

awsr() {
  aws --region "$AWS_REGION" "$@"
}

kube() {
  kubectl --request-timeout="$KUBE_TIMEOUT" "$@"
}

ansible_run() {
  (cd "$REPO_ROOT" && ansible-playbook "$@")
}

run_quiet() {
  local log="$TMP_DIR/command.log"
  if "$@" >"$log" 2>&1; then
    return 0
  fi
  tail -n 15 "$log" | sed 's/^/    /'
  return 1
}

iso() {
  date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ
}

epoch() {
  [ -n "$1" ] && date -u -d "$1" +%s 2>/dev/null
}

number() {
  awk -v value="$1" -v scale="${2:-1}" 'BEGIN { printf "%.0f", value * scale }'
}

last_line() {
  printf '%s\n' "$1" | awk 'NF { line = $0 } END { print line }'
}

ensure_kube() {
  case "$KUBE_STATE" in
    ok) return 0 ;;
    failed)
      skip "EKS API not reachable; see the first Kubernetes section"
      return 1
      ;;
  esac

  KUBE_STATE=failed
  if ! has_command kubectl || ! has_command ansible-playbook; then
    fail "kubectl and ansible-playbook are needed for the Kubernetes checks"
    return 1
  fi

  note "Opening the SSM tunnel to the private EKS API (ansible/playbooks/kubeconfig.yml)..."
  if ! run_quiet ansible_run ansible/playbooks/kubeconfig.yml; then
    fail "Could not reach the EKS API through the SSM tunnel"
    return 1
  fi

  KUBE_STATE=ok
  ok "kubectl context $(kubectl config current-context)"
}

load_alb() {
  if [ "$ALB_LOOKED_UP" = false ]; then
    ALB_LOOKED_UP=true
    local row arn resource tgs
    if row="$(
      awsr elbv2 describe-load-balancers \
        --names "$ALB_NAME" \
        --query 'LoadBalancers[0].[LoadBalancerArn, DNSName, State.Code]' \
        --output text 2>&1
    )"; then
      read -r ALB_ARN ALB_DNS ALB_STATE <<<"$row"
    elif [[ "$row" == *LoadBalancerNotFound* ]]; then
      ALB_PROBLEM="Load balancer $ALB_NAME not found; the Load Balancer Controller creates it from the Ingress"
    else
      ALB_PROBLEM="Could not look up load balancer $ALB_NAME: $(last_line "$row")"
    fi

    if [ -n "$ALB_ARN" ]; then
      if tgs="$(
        awsr elbv2 describe-target-groups \
          --load-balancer-arn "$ALB_ARN" \
          --query 'TargetGroups[].TargetGroupArn' \
          --output text 2>&1
      )"; then
        read -ra TG_ARNS <<<"$tgs"
      else
        TG_PROBLEM="Could not list the ALB target groups: $(last_line "$tgs")"
      fi
      for arn in "${TG_ARNS[@]}"; do
        resource="${arn##*:targetgroup/}"
        TG_LABEL[$arn]="${resource%%/*}"
      done
      if [ ${#TG_ARNS[@]} -gt 0 ]; then
        while IFS=$'\t' read -r arn resource; do
          if [ -n "$resource" ] && [ "$resource" != None ]; then
            resource="${resource#"$NAMESPACE/$INGRESS_NAME-"}"
            TG_LABEL[$arn]="${resource%:*}"
          fi
        done < <(
          awsr elbv2 describe-tags \
            --resource-arns "${TG_ARNS[@]}" \
            --query "TagDescriptions[].[ResourceArn, Tags[?Key=='ingress.k8s.aws/resource'].Value | [0]]" \
            --output text 2>/dev/null || true
        )
      fi
    fi
  fi
  [ -n "$ALB_ARN" ]
}

http_probe() {
  curl -sS -o /dev/null --max-time 15 \
    -w '%{http_code}|%{time_total}|%{redirect_url}|%{content_type}' "$1" 2>/dev/null || true
}

check_pods() {
  local namespace="$1" rows name phase ready restarts waiting last_reason last_at
  local total=0 bad=0 count latest at item items

  rows="$(
    kube get pods -n "$namespace" --no-headers \
      -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount,WAITING:.status.containerStatuses[*].state.waiting.reason,LAST:.status.containerStatuses[*].lastState.terminated.reason,LAST_AT:.status.containerStatuses[*].lastState.terminated.finishedAt' 2>&1
  )" || {
    fail "Could not list pods in $namespace: $(last_line "$rows")"
    return 1
  }
  if [ -z "$rows" ] || [[ "$rows" == "No resources found"* ]]; then
    warn "No pods in $namespace"
    return 0
  fi

  while read -r name phase ready restarts waiting last_reason last_at; do
    [ "$phase" = Succeeded ] && continue
    total=$((total + 1))

    if [ "$phase" != Running ] || ! [[ "$ready" =~ ^true(,true)*$ ]]; then
      bad=$((bad + 1))
      if [ "$waiting" != "<none>" ]; then
        fail "$namespace/$name is $phase and not ready ($waiting)"
      else
        fail "$namespace/$name is $phase and not ready"
      fi
      kube describe pod -n "$namespace" "$name" 2>/dev/null | sed -n '/^Events:/,$p' | tail -n 12 | sed 's/^/    /'
    fi

    count=0
    IFS=, read -ra items <<<"$restarts"
    for item in "${items[@]}"; do
      [[ "$item" =~ ^[0-9]+$ ]] && count=$((count + item))
    done
    if [ "$count" -gt 0 ]; then
      latest=0
      IFS=, read -ra items <<<"$last_at"
      for item in "${items[@]}"; do
        at="$(epoch "$item")" || continue
        [ "$at" -gt "$latest" ] && latest="$at"
      done
      if [ "$latest" -ge "$WINDOW_START" ]; then
        warn "$namespace/$name restarted $count time(s), last at $(iso "$latest") ($last_reason)"
      else
        note "$namespace/$name restarted $count time(s), none in the last ${LOOKBACK_HOURS}h"
      fi
    fi
  done <<<"$rows"

  if [ "$bad" -eq 0 ]; then
    ok "All $total pods in $namespace are running and ready"
  fi
}

section_endpoints() {
  section "Public endpoints"
  local target="" addresses probe code seconds redirect type path
  local cert not_after not_after_epoch issuer days acm status acm_after renewal in_use

  sub "DNS"
  if has_command dig; then
    target="$(dig +short "$APP_DOMAIN" CNAME | head -n 1)"
    target="${target%.}"
    addresses="$(dig +short "$APP_DOMAIN" A | grep -E '^[0-9.]+$' | tr '\n' ' ')"
  else
    addresses="$(getent ahostsv4 "$APP_DOMAIN" | awk '{ print $1 }' | sort -u | tr '\n' ' ')"
  fi
  printf '%-10s %s\n' "CNAME" "${target:-none}" "Addresses" "${addresses:-none}"

  if [ -z "$addresses" ]; then
    fail "$APP_DOMAIN does not resolve"
  fi
  if ! has_command dig; then
    note "dig is not installed; skipping the CNAME check"
  elif ! load_alb; then
    warn "DNS target not checked: $ALB_PROBLEM"
  elif [ -z "$target" ]; then
    fail "$APP_DOMAIN has no CNAME record; run ansible-playbook ansible/playbooks/cloudflare-dns.yml"
  elif [ "${target,,}" = "${ALB_DNS,,}" ]; then
    ok "$APP_DOMAIN points at the ALB"
  else
    fail "$APP_DOMAIN points at $target but the ALB is $ALB_DNS; run ansible-playbook ansible/playbooks/cloudflare-dns.yml"
  fi

  sub "HTTP"
  if ! has_command curl; then
    skip "curl is not installed"
  else
    probe="$(http_probe "http://$APP_DOMAIN/")"
    IFS='|' read -r code seconds redirect type <<<"$probe"
    printf '%-44s %s  %ss  -> %s\n' "http://$APP_DOMAIN/" "$code" "$seconds" "${redirect:-none}"
    if [[ "$code" =~ ^30[1278]$ ]] && [[ "$redirect" == https://* ]]; then
      ok "HTTP redirects to HTTPS"
    else
      warn "http://$APP_DOMAIN/ returned $code instead of a redirect to HTTPS"
    fi

    for path in / /health; do
      probe="$(http_probe "$APP_URL$path")"
      IFS='|' read -r code seconds redirect type <<<"$probe"
      printf '%-44s %s  %ss\n' "$APP_URL$path" "$code" "$seconds"
      if [ "$code" = 200 ]; then
        ok "Frontend $path answered 200 in ${seconds}s"
      else
        fail "Frontend $path returned $code"
      fi
    done

    probe="$(http_probe "$APP_URL/api/health")"
    IFS='|' read -r code seconds redirect type <<<"$probe"
    printf '%-44s %s  %ss  %s\n' "$APP_URL/api/health" "$code" "$seconds" "${type:-}"
    if [ "$code" != 000 ] && [ "$code" -lt 500 ] && [[ "$type" == *json* ]]; then
      ok "API requests reach the backend (HTTP $code from the app; /api/health is not a backend route)"
    elif [ "$code" = 000 ] || [ "$code" -ge 500 ]; then
      fail "$APP_URL/api/health returned $code; the backend isn't answering through the ALB"
    else
      warn "$APP_URL/api/health returned $code ($type), not a JSON answer from the backend"
    fi
  fi

  sub "TLS certificate"
  if ! has_command openssl; then
    skip "openssl is not installed"
  else
    cert="$(
      timeout 20 openssl s_client -connect "$APP_DOMAIN:443" -servername "$APP_DOMAIN" </dev/null 2>/dev/null \
        | openssl x509 -noout -enddate -issuer 2>/dev/null
    )"
    not_after="$(sed -n 's/^notAfter=//p' <<<"$cert")"
    issuer="$(sed -n 's/^issuer=//p' <<<"$cert")"
    not_after_epoch="$(epoch "$not_after")"
    if [ -z "$not_after_epoch" ]; then
      fail "Could not read the TLS certificate served on $APP_DOMAIN:443"
    else
      days=$(((not_after_epoch - NOW) / 86400))
      printf '%-10s %s\n%-10s %s\n' "Expires" "$not_after" "Issuer" "$issuer"
      if [ "$days" -lt 7 ]; then
        fail "Served certificate expires in $days days"
      elif [ "$days" -lt 30 ]; then
        warn "Served certificate expires in $days days; ACM normally renews 60 days ahead"
      else
        ok "Served certificate is valid for $days more days"
      fi
    fi
  fi

  if [ -n "$ACM_CERT_ARN" ]; then
    if acm="$(
      awsr acm describe-certificate \
        --certificate-arn "$ACM_CERT_ARN" \
        --query 'Certificate.[Status, NotAfter, RenewalEligibility, length(InUseBy)]' \
        --output text 2>&1
    )"; then
      read -r status acm_after renewal in_use <<<"$acm"
      printf 'ACM: %s, expires %s, renewal %s, attached to %s resource(s)\n' "$status" "$acm_after" "$renewal" "$in_use"
      if [ "$status" = ISSUED ]; then
        ok "ACM certificate is ISSUED"
      else
        fail "ACM certificate status is $status"
      fi
      if [ "$in_use" = 0 ]; then
        warn "ACM certificate isn't attached to any load balancer"
      elif [ "$renewal" != ELIGIBLE ]; then
        warn "ACM certificate renewal eligibility is $renewal"
      fi
    else
      fail "Could not describe the ACM certificate: $(last_line "$acm")"
    fi
  fi
}

section_alarms() {
  section "CloudWatch alarms"
  local alarms name state since actions reason total=0 healthy=0 silent=0 history

  if [ "$REFRESH_ALARMS" = false ]; then
    note "Alarms not refreshed; run with --refresh-alarms after the target groups change."
  elif ! has_command ansible-playbook; then
    skip "ansible-playbook is not installed; ALB alarms not refreshed"
  else
    sub "Refresh ALB alarms (ansible/playbooks/monitoring.yml)"
    if run_quiet ansible_run ansible/playbooks/monitoring.yml; then
      ok "ALB 5xx and unhealthy-target alarms match the current target groups"
    else
      fail "Refreshing the ALB alarms failed"
    fi
  fi

  sub "Current state"
  if ! alarms="$(
    awsr cloudwatch describe-alarms \
      --alarm-name-prefix "$ALARM_PREFIX" \
      --query 'MetricAlarms[].[AlarmName, StateValue, StateUpdatedTimestamp, length(AlarmActions), StateReason]' \
      --output text 2>&1
  )"; then
    fail "Could not read CloudWatch alarms: $(last_line "$alarms")"
    return
  fi
  if [ -z "$alarms" ]; then
    warn "No CloudWatch alarms start with $ALARM_PREFIX"
  else
    while IFS=$'\t' read -r name state since actions reason; do
      total=$((total + 1))
      printf '%-18s %-20s %s\n' "$state" "${since%%.*}" "$name"
      case "$state" in
        OK) healthy=$((healthy + 1)) ;;
        ALARM) fail "$name is in ALARM: $reason" ;;
        *) warn "$name is $state: $reason" ;;
      esac
      [ "$actions" = 0 ] && silent=$((silent + 1))
    done <<<"$alarms"
    if [ "$healthy" -eq "$total" ]; then
      ok "All $total alarms are OK"
    fi
    if [ "$silent" -gt 0 ]; then
      note "$silent of $total alarms have no notification action (monitoring_alert_sns_topic_arn adds one to the ALB alarms)."
    fi
  fi

  sub "State changes in the last ${LOOKBACK_HOURS}h"
  history="$(
    awsr cloudwatch describe-alarm-history \
      --history-item-type StateUpdate \
      --start-date "$(iso "$WINDOW_START")" \
      --query "AlarmHistoryItems[?starts_with(AlarmName, '$ALARM_PREFIX')].[Timestamp, AlarmName, HistorySummary]" \
      --output text 2>&1
  )" || {
    warn "Could not read alarm history: $(last_line "$history")"
    return
  }
  if [ -z "$history" ]; then
    note "No state changes."
  else
    sort -r <<<"$history" | head -n 20 | awk -F'\t' '{ printf "%-20s %s: %s\n", substr($1, 1, 19), $2, $3 }'
  fi
}

alb_metric() {
  local id="$1" label="$2" metric="$3" stat="$4" period="$5" dims="" dim
  shift 5
  for dim in "$@"; do
    dims+="${dims:+,}{\"Name\":\"${dim%%=*}\",\"Value\":\"${dim#*=}\"}"
  done
  printf '{"Id":"%s","Label":"%s","MetricStat":{"Metric":{"Namespace":"AWS/ApplicationELB","MetricName":"%s","Dimensions":[%s]},"Period":%s,"Stat":"%s"}}' \
    "$id" "$label" "$metric" "$dims" "$period" "$stat"
}

section_metrics() {
  section "ALB metrics (last ${LOOKBACK_HOURS}h)"
  if ! load_alb; then
    fail "$ALB_PROBLEM"
    return
  fi

  local period=$((LOOKBACK_HOURS * 3600)) end=$((NOW / 60 * 60)) lb="${ALB_ARN#*:loadbalancer/}"
  local queries=() index=0 arn tg label rows id total peak value before="${#RESULTS[@]}"
  local lb_dim="LoadBalancer=$lb"
  [ -n "$TG_PROBLEM" ] && fail "$TG_PROBLEM; per-target-group metrics are missing"

  queries+=(
    "$(alb_metric requests "Requests" RequestCount Sum "$period" "$lb_dim")"
    "$(alb_metric elb5xx "5xx from the ALB" HTTPCode_ELB_5XX_Count Sum "$period" "$lb_dim")"
    "$(alb_metric elb4xx "4xx from the ALB" HTTPCode_ELB_4XX_Count Sum "$period" "$lb_dim")"
    "$(alb_metric target5xx "5xx from targets" HTTPCode_Target_5XX_Count Sum "$period" "$lb_dim")"
    "$(alb_metric target4xx "4xx from targets" HTTPCode_Target_4XX_Count Sum "$period" "$lb_dim")"
    "$(alb_metric latency "Response time avg (ms)" TargetResponseTime Average "$period" "$lb_dim")"
    "$(alb_metric latencyp99 "Response time p99 (ms)" TargetResponseTime p99 "$period" "$lb_dim")"
    "$(alb_metric connerrors "Target connection errors" TargetConnectionErrorCount Sum "$period" "$lb_dim")"
    "$(alb_metric rejected "Rejected connections" RejectedConnectionCount Sum "$period" "$lb_dim")"
  )
  for arn in "${TG_ARNS[@]}"; do
    index=$((index + 1))
    tg="${arn##*:}"
    label="${TG_LABEL[$arn]:-$tg}"
    queries+=(
      "$(alb_metric "tgrequests$index" "Requests to $label" RequestCount Sum "$period" "$lb_dim" "TargetGroup=$tg")"
      "$(alb_metric "tg5xx$index" "5xx from $label" HTTPCode_Target_5XX_Count Sum "$period" "$lb_dim" "TargetGroup=$tg")"
      "$(alb_metric "tgunhealthy$index" "Most unhealthy $label targets" UnHealthyHostCount Maximum "$period" "$lb_dim" "TargetGroup=$tg")"
    )
  done

  rows="$(
    awsr cloudwatch get-metric-data \
      --metric-data-queries "[$(IFS=,; printf '%s' "${queries[*]}")]" \
      --start-time "$(iso $((end - period)))" \
      --end-time "$(iso "$end")" \
      --query 'MetricDataResults[].[Id, Label, sum(Values), max(Values)]' \
      --output text 2>&1
  )" || {
    fail "Could not read ALB metrics: $(last_line "$rows")"
    return
  }

  while IFS=$'\t' read -r id label total peak; do
    case "$id" in
      latency*)
        if [ "$peak" = None ]; then value="no data"; else value="$(number "$peak" 1000)"; fi
        ;;
      tgunhealthy*)
        if [ "$peak" = None ]; then value="no data"; else value="$(number "$peak")"; fi
        ;;
      *) value="$(number "$total")" ;;
    esac
    printf '%-40s %s\n' "$label" "$value"

    case "$id" in
      elb5xx | tg5xx* | connerrors | rejected)
        [ "$value" -gt 0 ] && warn "$label: $value in the last ${LOOKBACK_HOURS}h"
        ;;
      tgunhealthy*)
        [ "$value" != "no data" ] && [ "$value" -gt 0 ] && warn "$label: up to $value in the last ${LOOKBACK_HOURS}h"
        ;;
      requests)
        [ "$value" -eq 0 ] && note "No requests reached the ALB in this window."
        ;;
    esac
  done <<<"$rows"

  if [ "${#RESULTS[@]}" -eq "$before" ]; then
    ok "No ALB 5xx, connection errors or unhealthy targets in the last ${LOOKBACK_HOURS}h"
  fi
}

section_alb() {
  section "Load balancer and target health"
  local arn label health id port state reason total unhealthy reasons listeners

  if ! load_alb; then
    fail "$ALB_PROBLEM"
    return
  fi

  printf '%-8s %s\n' "Name" "$ALB_NAME" "State" "$ALB_STATE" "DNS" "$ALB_DNS"
  if [ "$ALB_STATE" = active ]; then
    ok "ALB is active"
  else
    fail "ALB state is $ALB_STATE"
  fi

  sub "Listeners"
  if listeners="$(
    awsr elbv2 describe-listeners \
      --load-balancer-arn "$ALB_ARN" \
      --query 'Listeners[].[Port, Protocol, DefaultActions[0].Type, SslPolicy]' \
      --output text 2>&1
  )"; then
    awk -F'\t' '{ printf "%-6s %-6s %-16s %s\n", $1, $2, $3, ($4 == "None" ? "" : $4) }' <<<"$listeners"
    if ! awk -F'\t' '$1 == 443 && $2 == "HTTPS" { found = 1 } END { exit !found }' <<<"$listeners"; then
      fail "The ALB has no HTTPS listener on 443"
    fi
  else
    fail "Could not list ALB listeners: $(last_line "$listeners")"
  fi

  if [ ${#TG_ARNS[@]} -eq 0 ]; then
    fail "${TG_PROBLEM:-No target groups are attached to the ALB}"
    return
  fi

  for arn in "${TG_ARNS[@]}"; do
    label="${TG_LABEL[$arn]}"
    sub "Targets: $label"
    health="$(
      awsr elbv2 describe-target-health \
        --target-group-arn "$arn" \
        --query 'TargetHealthDescriptions[].[Target.Id, Target.Port, TargetHealth.State, TargetHealth.Reason]' \
        --output text 2>&1
    )" || {
      fail "Could not read target health for $label: $(last_line "$health")"
      continue
    }
    total=0
    unhealthy=0
    reasons=""
    while IFS=$'\t' read -r id port state reason; do
      [ -z "$id" ] && continue
      total=$((total + 1))
      printf '%-16s %-6s %-10s %s\n' "$id" "$port" "$state" "$([ "$reason" = None ] || printf '%s' "$reason")"
      if [ "$state" != healthy ]; then
        unhealthy=$((unhealthy + 1))
        reasons="${reasons:+$reasons, }$state/$reason"
      fi
    done <<<"$health"
    if [ "$total" -eq 0 ]; then
      fail "$label has no registered targets"
    elif [ "$unhealthy" -gt 0 ]; then
      fail "$label: $unhealthy of $total targets not healthy ($reasons)"
    else
      ok "$label: $total/$total targets healthy"
    fi
  done
}

section_eks() {
  section "EKS (AWS side)"
  local info status version platform issues insights name addons addon nodegroups ng row min desired max release asg
  local instances in_service metrics ops ping agent last_ping state system_status instance_status

  sub "Control plane"
  info="$(
    awsr eks describe-cluster \
      --name "$EKS_CLUSTER_NAME" \
      --query 'cluster.[status, version, platformVersion, length(health.issues || `[]`)]' \
      --output text 2>&1
  )" || {
    fail "Cluster $EKS_CLUSTER_NAME not found: $(last_line "$info")"
    return
  }
  read -r status version platform issues <<<"$info"
  printf '%-10s %s\n' "Status" "$status" "Version" "$version" "Platform" "$platform"
  if [ "$status" = ACTIVE ]; then
    ok "Cluster $EKS_CLUSTER_NAME is ACTIVE on Kubernetes $version"
  else
    fail "Cluster $EKS_CLUSTER_NAME is $status"
  fi
  if [ "$issues" != 0 ]; then
    fail "Cluster reports $issues health issue(s)"
    awsr eks describe-cluster --name "$EKS_CLUSTER_NAME" \
      --query 'cluster.health.issues[].[code, message]' --output text | sed 's/^/    /'
  fi

  sub "Upgrade insights"
  if insights="$(
    awsr eks list-insights \
      --cluster-name "$EKS_CLUSTER_NAME" \
      --query 'insights[].[insightStatus.status, kubernetesVersion, name]' \
      --output text 2>&1
  )"; then
    if [ -z "$insights" ]; then
      note "No insights."
    else
      awk -F'\t' '{ printf "%-9s %-6s %s\n", $1, $2, $3 }' <<<"$insights"
      while IFS=$'\t' read -r status version name; do
        [ "$status" = PASSING ] || warn "Upgrade insight '$name' is $status for $version"
      done <<<"$insights"
    fi
  else
    warn "Could not list upgrade insights: $(last_line "$insights")"
  fi

  sub "Managed add-ons"
  if ! addons="$(awsr eks list-addons --cluster-name "$EKS_CLUSTER_NAME" --query 'addons' --output text 2>&1)"; then
    fail "Could not list the EKS add-ons: $(last_line "$addons")"
  elif [ -z "$addons" ]; then
    warn "No managed add-ons; terraform/eks.tf manages vpc-cni, coredns and kube-proxy, so run ./scripts/tf.sh apply"
  else
    for addon in $addons; do
      if ! row="$(
        awsr eks describe-addon --cluster-name "$EKS_CLUSTER_NAME" --addon-name "$addon" \
          --query 'addon.[status, addonVersion, length(health.issues || `[]`)]' --output text 2>&1
      )"; then
        fail "Could not describe add-on $addon: $(last_line "$row")"
        continue
      fi
      read -r status version issues <<<"$row"
      printf '%-24s %-10s %s\n' "$addon" "$status" "$version"
      if [ "$status" = ACTIVE ] && [ "$issues" = 0 ]; then
        ok "Add-on $addon is ACTIVE"
      else
        fail "Add-on $addon is $status with $issues issue(s)"
      fi
    done
  fi

  if ! nodegroups="$(awsr eks list-nodegroups --cluster-name "$EKS_CLUSTER_NAME" --query 'nodegroups' --output text 2>&1)"; then
    fail "Could not list the node groups: $(last_line "$nodegroups")"
    nodegroups=""
  elif [ -z "$nodegroups" ]; then
    fail "Cluster $EKS_CLUSTER_NAME has no node groups"
  fi
  for ng in $nodegroups; do
    sub "Node group $ng"
    row="$(
      awsr eks describe-nodegroup --cluster-name "$EKS_CLUSTER_NAME" --nodegroup-name "$ng" \
        --query 'nodegroup.[status, scalingConfig.minSize, scalingConfig.desiredSize, scalingConfig.maxSize, releaseVersion, length(health.issues || `[]`), resources.autoScalingGroups[0].name]' \
        --output text 2>&1
    )" || {
      fail "Could not describe node group $ng: $(last_line "$row")"
      continue
    }
    read -r status min desired max release issues asg <<<"$row"
    printf '%-10s %s\n' "Status" "$status" "Scaling" "min $min / desired $desired / max $max" "AMI" "$release" "ASG" "$asg"
    if [ "$status" = ACTIVE ]; then
      ok "Node group $ng is ACTIVE with $desired desired node(s)"
    else
      fail "Node group $ng is $status"
    fi
    if [ "$desired" = 0 ]; then
      warn "Node group $ng is scaled to 0 nodes"
    fi
    if [ "$issues" != 0 ]; then
      fail "Node group $ng reports $issues health issue(s)"
      awsr eks describe-nodegroup --cluster-name "$EKS_CLUSTER_NAME" --nodegroup-name "$ng" \
        --query 'nodegroup.health.issues[].[code, message]' --output text | sed 's/^/    /'
    fi

    if [ -n "$asg" ] && [ "$asg" != None ]; then
      if ! instances="$(
        awsr autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$asg" \
          --query 'AutoScalingGroups[0].Instances[].[InstanceId, InstanceType, AvailabilityZone, LifecycleState, HealthStatus]' \
          --output text 2>&1
      )"; then
        fail "Could not read the instances in $asg: $(last_line "$instances")"
      else
        [ -n "$instances" ] && awk -F'\t' '{ printf "  %-20s %-10s %-12s %-10s %s\n", $1, $2, $3, $4, $5 }' <<<"$instances"
        in_service="$(awk -F'\t' '$4 == "InService" && $5 == "Healthy"' <<<"$instances" | grep -c . || true)"
        if [ "$in_service" -lt "$desired" ]; then
          fail "Only $in_service of $desired node instances are InService and Healthy"
        fi
      fi

      if ! metrics="$(
        awsr autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$asg" \
          --query 'AutoScalingGroups[0].EnabledMetrics[].Metric' --output text 2>&1
      )"; then
        warn "Could not read the group metrics on $asg: $(last_line "$metrics")"
      elif [[ " ${metrics//$'\t'/ } " != *" GroupInServiceInstances "* ]]; then
        warn "Group metrics are off on $asg, so $ALARM_PREFIX-nodegroup-no-running-nodes never gets data and stays OK (fix: aws autoscaling enable-metrics-collection --auto-scaling-group-name $asg --granularity 1Minute --metrics GroupInServiceInstances)"
      fi
    fi
  done

  sub "Ops instance (SSM path to the private API)"
  if [ -z "$OPS_INSTANCE_ID" ]; then
    skip "ops_instance_id is not set"
    return
  fi
  ops="$(
    awsr ec2 describe-instance-status --instance-ids "$OPS_INSTANCE_ID" --include-all-instances \
      --query 'InstanceStatuses[0].[InstanceState.Name, SystemStatus.Status, InstanceStatus.Status]' \
      --output text 2>&1
  )" || {
    fail "Ops instance $OPS_INSTANCE_ID not found: $(last_line "$ops")"
    return
  }
  read -r state system_status instance_status <<<"$ops"
  printf '%-10s %s\n' "Instance" "$OPS_INSTANCE_ID" "State" "$state" "Checks" "system $system_status, instance $instance_status"
  if [ "$state" = running ] && [ "$system_status" = ok ] && [ "$instance_status" = ok ]; then
    ok "Ops instance is running and passes its status checks"
  elif [ "$state" = running ]; then
    warn "Ops instance status checks: system $system_status, instance $instance_status"
  else
    fail "Ops instance is $state"
  fi

  if ! ping="$(
    awsr ssm describe-instance-information --filters "Key=InstanceIds,Values=$OPS_INSTANCE_ID" \
      --query 'InstanceInformationList[0].[PingStatus, AgentVersion, LastPingDateTime]' \
      --output text 2>&1
  )"; then
    fail "Could not read the SSM agent status of the ops instance: $(last_line "$ping")"
    return
  fi
  read -r ping agent last_ping <<<"$ping"
  if [ -z "$ping" ] || [ "$ping" = None ]; then
    fail "Ops instance is not registered with SSM; kubectl and deploys can't reach the cluster"
  elif [ "$ping" = Online ]; then
    ok "SSM agent $agent is Online"
  else
    fail "SSM agent is ${ping:-unknown} (last ping ${last_ping:-never}); kubectl and deploys can't reach the cluster"
  fi
}

section_nodes() {
  section "Kubernetes nodes"
  ensure_kube || return
  local readyz rows name ready memory disk pid pods cordoned count placements total=0
  declare -A node_pods=() node_capacity=()

  readyz="$(kube get --raw /readyz 2>&1)"
  if [ "$readyz" = ok ]; then
    ok "API server /readyz is ok"
  else
    fail "API server /readyz: $(last_line "$readyz")"
  fi

  sub "Nodes"
  kube get nodes -o wide
  if ! rows="$(
    kube get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.conditions[?(@.type=="Ready")].status}{"|"}{.status.conditions[?(@.type=="MemoryPressure")].status}{"|"}{.status.conditions[?(@.type=="DiskPressure")].status}{"|"}{.status.conditions[?(@.type=="PIDPressure")].status}{"|"}{.status.allocatable.pods}{"|"}{.spec.unschedulable}{"\n"}{end}' 2>&1
  )"; then
    fail "Could not read the node conditions: $(last_line "$rows")"
    return
  fi
  while IFS='|' read -r name ready memory disk pid pods cordoned; do
    [ -z "$name" ] && continue
    total=$((total + 1))
    node_capacity[$name]="$pods"
    [ "$ready" = True ] || fail "Node $name is not Ready ($ready)"
    [ "$memory$disk$pid" = FalseFalseFalse ] || warn "Node $name pressure: memory=$memory disk=$disk pid=$pid"
    [ "$cordoned" = true ] && warn "Node $name is cordoned"
  done <<<"$rows"
  if [ "$total" -eq 0 ]; then
    fail "The cluster has no nodes"
    return
  fi

  sub "Usage"
  kube top nodes || warn "kubectl top nodes failed; metrics-server may not be ready"

  sub "Pod capacity"
  if ! placements="$(
    kube get pods -A --field-selector=status.phase!=Succeeded,status.phase!=Failed \
      -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' 2>&1
  )"; then
    warn "Could not count the pods on each node: $(last_line "$placements")"
  else
    while read -r count name; do
      node_pods[$name]="$count"
    done < <(awk 'NF' <<<"$placements" | sort | uniq -c)
    for name in "${!node_capacity[@]}"; do
      count="${node_pods[$name]:-0}"
      pods="${node_capacity[$name]}"
      printf '%-48s %s/%s pods\n' "$name" "$count" "$pods"
      if [[ "$pods" =~ ^[1-9][0-9]*$ ]] && [ $((count * 100 / pods)) -ge 90 ]; then
        warn "Node $name runs $count of its $pods pod slots; new pods may not schedule"
      fi
    done
  fi

  sub "Requested resources"
  kube describe nodes | awk '/^Name:/ { print; next } /^Allocated resources:/ { show = 1 } /^Events:/ { show = 0 } show'
}

section_system() {
  section "Cluster add-ons (kube-system)"
  ensure_kube || return
  local name jsonpath row ready want available releases failed logs errors

  sub "Pods"
  kube get pods -n kube-system -o wide
  check_pods kube-system

  sub "Controllers"
  for name in deployment/aws-load-balancer-controller deployment/metrics-server deployment/coredns daemonset/aws-node daemonset/kube-proxy; do
    if [[ "$name" == deployment/* ]]; then
      jsonpath='{.status.readyReplicas}|{.spec.replicas}'
    else
      jsonpath='{.status.numberReady}|{.status.desiredNumberScheduled}'
    fi
    row="$(kube get "$name" -n kube-system -o jsonpath="$jsonpath" 2>&1)" || {
      fail "Could not read $name in kube-system: $(last_line "$row")"
      continue
    }
    IFS='|' read -r ready want <<<"$row"
    ready="${ready:-0}"
    if [ "$ready" = "$want" ] && [ "$want" != 0 ]; then
      ok "$name: $ready/$want ready"
    else
      fail "$name: $ready/$want ready"
    fi
  done

  if ! available="$(
    kube get apiservice v1beta1.metrics.k8s.io \
      -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>&1
  )"; then
    fail "Could not read the metrics APIService: $(last_line "$available")"
  elif [ "$available" = True ]; then
    ok "Metrics API is available"
  else
    fail "Metrics API is not available (Available=${available:-unknown}); HPAs and kubectl top won't work"
  fi

  sub "Helm releases"
  if ! has_command helm; then
    skip "helm is not installed"
  elif ! releases="$(helm list -A 2>&1)"; then
    fail "Could not list the Helm releases: $(last_line "$releases")"
  else
    printf '%s\n' "$releases"
    if ! failed="$(helm list -A --failed --pending -q 2>&1)"; then
      fail "Could not list failed or pending Helm releases: $(last_line "$failed")"
    elif [ -n "$failed" ]; then
      fail "Helm releases not deployed: $(tr '\n' ' ' <<<"$failed")"
    else
      ok "All Helm releases are deployed"
    fi
  fi

  sub "Load Balancer Controller errors (last 300 log lines per pod)"
  if ! logs="$(
    kube logs -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller \
      --tail=300 --prefix 2>&1
  )"; then
    warn "Could not read the Load Balancer Controller logs: $(last_line "$logs")"
  elif [ -z "$logs" ] || [[ "$logs" == "No resources found"* ]]; then
    warn "No Load Balancer Controller log lines to check; see the controller check above"
  else
    errors="$(grep '"level":"error"' <<<"$logs" || true)"
    if [ -z "$errors" ]; then
      ok "No errors in the Load Balancer Controller logs"
    else
      tail -n 5 <<<"$errors" | cut -c1-400
      warn "Load Balancer Controller logged $(grep -c . <<<"$errors") error line(s)"
    fi
  fi
}

section_workloads() {
  section "Application workloads ($NAMESPACE)"
  ensure_kube || return
  local deployment row desired ready updated image status history name current max active hpas
  local services svc endpoints hostname refs ref answer keys policy can_patch

  if ! answer="$(kube get namespace "$NAMESPACE" -o name 2>&1)"; then
    fail "Could not read namespace $NAMESPACE: $(last_line "$answer")"
    return
  fi

  sub "Deployments"
  kube get deployments -n "$NAMESPACE" -o wide
  for deployment in "$BACKEND_DEPLOYMENT" "$FRONTEND_DEPLOYMENT"; do
    row="$(
      kube get deployment "$deployment" -n "$NAMESPACE" \
        -o jsonpath='{.spec.replicas}|{.status.readyReplicas}|{.status.updatedReplicas}|{.spec.template.spec.containers[0].image}' 2>&1
    )" || {
      fail "Could not read Deployment $deployment: $(last_line "$row")"
      continue
    }
    IFS='|' read -r desired ready updated image <<<"$row"
    ready="${ready:-0}"
    if [ "$desired" = 0 ]; then
      warn "$deployment is scaled to 0"
    elif [ "$ready" -lt "$desired" ]; then
      fail "$deployment: $ready/$desired ready, ${updated:-0} updated"
    else
      ok "$deployment: $ready/$desired ready on ${image##*:}"
    fi
    status="$(kube rollout status "deployment/$deployment" -n "$NAMESPACE" --watch=false 2>&1)"
    if [[ "$status" == *"exceeded its progress deadline"* ]]; then
      fail "$deployment rollout exceeded its progress deadline; roll back with kubectl rollout undo deployment/$deployment -n $NAMESPACE"
    elif [[ "$status" != *"successfully rolled out"* ]]; then
      warn "$deployment rollout: $(last_line "$status")"
    fi
  done

  sub "Rollout history (ReplicaSets, oldest first)"
  for deployment in "$BACKEND_DEPLOYMENT" "$FRONTEND_DEPLOYMENT"; do
    history="$(
      kube get replicasets -n "$NAMESPACE" -l "app.kubernetes.io/name=$deployment" \
        --sort-by=.metadata.creationTimestamp \
        -o custom-columns='REVISION:.metadata.annotations.deployment\.kubernetes\.io/revision,DESIRED:.spec.replicas,READY:.status.readyReplicas,CREATED:.metadata.creationTimestamp,IMAGE:.spec.template.spec.containers[0].image' 2>&1
    )"
    printf '%s\n' "$deployment"
    head -n 1 <<<"$history"
    tail -n +2 <<<"$history" | tail -n 5
  done

  sub "Pods"
  kube get pods -n "$NAMESPACE" -o wide
  check_pods "$NAMESPACE"

  sub "Autoscaling"
  kube get hpa -n "$NAMESPACE"
  if ! hpas="$(
    kube get hpa -n "$NAMESPACE" \
      -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.currentReplicas}{"|"}{.spec.maxReplicas}{"|"}{.status.conditions[?(@.type=="ScalingActive")].status}{"\n"}{end}' 2>&1
  )"; then
    warn "Could not read the HPA status: $(last_line "$hpas")"
  elif [ -z "$hpas" ]; then
    warn "No HorizontalPodAutoscalers in $NAMESPACE"
  else
    while IFS='|' read -r name current max active; do
      [ -z "$name" ] && continue
      [ "$active" = True ] || warn "HPA $name can't compute its CPU metric (ScalingActive=${active:-unknown})"
      [ -n "$current" ] && [ "$current" = "$max" ] && warn "HPA $name is at its maximum of $max replicas"
    done <<<"$hpas"
  fi

  sub "Usage vs limits"
  usage_vs_limits "$NAMESPACE"

  sub "Services and endpoints"
  kube get services -n "$NAMESPACE" -o wide
  if ! services="$(kube get services -n "$NAMESPACE" -o jsonpath='{.items[*].metadata.name}' 2>&1)"; then
    fail "Could not list the services in $NAMESPACE: $(last_line "$services")"
    services=""
  elif [ -z "$services" ]; then
    fail "No services in $NAMESPACE"
  fi
  for svc in $services; do
    if ! endpoints="$(
      kube get endpointslices -n "$NAMESPACE" -l "kubernetes.io/service-name=$svc" \
        -o jsonpath='{range .items[*].endpoints[*]}{.conditions.ready}{"\n"}{end}' 2>&1
    )"; then
      fail "Could not read the endpoints of service $svc: $(last_line "$endpoints")"
      continue
    fi
    endpoints="$(grep -c '^true$' <<<"$endpoints" || true)"
    if [ "$endpoints" -gt 0 ]; then
      ok "Service $svc has $endpoints ready endpoint(s)"
    else
      fail "Service $svc has no ready endpoints"
    fi
  done

  sub "Ingress"
  kube get ingress "$INGRESS_NAME" -n "$NAMESPACE" -o wide
  if ! hostname="$(
    kube get ingress "$INGRESS_NAME" -n "$NAMESPACE" \
      -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>&1
  )"; then
    fail "Could not read Ingress $INGRESS_NAME: $(last_line "$hostname")"
  elif [ -n "$hostname" ]; then
    ok "Ingress $INGRESS_NAME has ALB $hostname"
  else
    fail "Ingress $INGRESS_NAME has no ALB hostname; check the Load Balancer Controller"
  fi
  kube get targetgroupbindings -n "$NAMESPACE" -o wide || warn "Could not list the TargetGroupBindings in $NAMESPACE"

  sub "Configuration"
  refs=""
  for deployment in "$BACKEND_DEPLOYMENT" "$FRONTEND_DEPLOYMENT"; do
    if ! row="$(
      kube get deployment "$deployment" -n "$NAMESPACE" \
        -o jsonpath='{range .spec.template.spec.containers[*].envFrom[*]}configmap/{.configMapRef.name}{"\n"}secret/{.secretRef.name}{"\n"}{end}' 2>&1
    )"; then
      fail "Could not read the ConfigMap and Secret references of $deployment: $(last_line "$row")"
      continue
    fi
    refs+="$row"$'\n'
  done
  refs="$(grep -v '/$' <<<"$refs" | sort -u || true)"
  for ref in $refs; do
    if ! answer="$(kube get "$ref" -n "$NAMESPACE" -o name 2>&1)"; then
      fail "$ref is referenced by a Deployment but could not be read: $(last_line "$answer")"
    elif [[ "$ref" == secret/* ]]; then
      if keys="$(
        kube get "$ref" -n "$NAMESPACE" \
          -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}' 2>&1
      )"; then
        ok "$ref exists with keys: ${keys:-none}"
      else
        warn "$ref exists but its keys could not be read: $(last_line "$keys")"
      fi
    else
      ok "$ref exists"
    fi
  done

  policy="$NAMESPACE-trusted-workloads"
  if ! answer="$(kube get validatingadmissionpolicy "$policy" -o name 2>&1)"; then
    fail "Could not read admission policy $policy: $(last_line "$answer")"
  elif ! answer="$(kube get validatingadmissionpolicybinding "$policy" -o name 2>&1)"; then
    fail "Could not read admission policy binding $policy: $(last_line "$answer")"
  else
    ok "Admission policy $policy and its binding are in place"
  fi

  if [ -n "$DEPLOY_GROUP" ]; then
    can_patch="$(kube auth can-i patch deployments.apps -n "$NAMESPACE" --as=monitoring-check --as-group="$DEPLOY_GROUP" 2>&1 || true)"
    case "$(last_line "$can_patch")" in
      yes) ok "Deploy group $DEPLOY_GROUP can patch Deployments" ;;
      no | "no "*) fail "Deploy group $DEPLOY_GROUP can't patch Deployments; CI/CD deploys will fail" ;;
      *) fail "Could not check whether $DEPLOY_GROUP can patch Deployments: $(last_line "$can_patch")" ;;
    esac
  fi
}

usage_vs_limits() {
  local namespace="$1" usage limits
  if ! usage="$(kube top pods -n "$namespace" --no-headers 2>&1)"; then
    warn "kubectl top pods failed: $(last_line "$usage")"
    return
  fi
  if ! limits="$(
    kube get pods -n "$namespace" --no-headers \
      -o custom-columns='NAME:.metadata.name,CPU:.spec.containers[0].resources.limits.cpu,MEMORY:.spec.containers[0].resources.limits.memory' 2>&1
  )"; then
    warn "Could not read the pod limits: $(last_line "$limits")"
    return
  fi
  relay < <(
    awk '
      function cpu(v) { if (v ~ /m$/) return v + 0; if (v ~ /^[0-9.]+$/) return v * 1000; return -1 }
      function mem(v) {
        if (v ~ /Ki$/) return v / 1024
        if (v ~ /Mi$/) return v + 0
        if (v ~ /Gi$/) return v * 1024
        if (v ~ /^[0-9]+$/) return v / 1048576
        return -1
      }
      function pct(used, limit) { return (used >= 0 && limit > 0) ? int(used * 100 / limit) : -1 }
      function show(p) { return p < 0 ? "-" : p "%" }
      NR == FNR { cpu_limit[$1] = $2; memory_limit[$1] = $3; next }
      FNR == 1 { printf "%-44s %8s %8s %5s %9s %9s %5s\n", "POD", "CPU", "LIMIT", "USE", "MEMORY", "LIMIT", "USE" }
      {
        c = pct(cpu($2), cpu(cpu_limit[$1])); m = pct(mem($3), mem(memory_limit[$1]))
        printf "%-44s %8s %8s %5s %9s %9s %5s\n", $1, $2, cpu_limit[$1], show(c), $3, memory_limit[$1], show(m)
        if (m >= 90) warnings[++n] = sprintf("WARN\t%s uses %d%% of its memory limit; it may be OOM-killed", $1, m)
        if (c >= 90) warnings[++n] = sprintf("WARN\t%s uses %d%% of its CPU limit and is being throttled", $1, c)
      }
      END { for (i = 1; i <= n; i++) print warnings[i] }
    ' <(printf '%s\n' "$limits") <(printf '%s\n' "$usage")
  )
}

metrics_summary() {
  awk -v pod="$1" -v now="$NOW" '
    function duration(s) {
      if (s >= 86400) return sprintf("%dd %dh", s / 86400, (s % 86400) / 3600)
      if (s >= 3600) return sprintf("%dh %dm", s / 3600, (s % 3600) / 60)
      return sprintf("%dm", s / 60)
    }
    /^#/ { next }
    {
      name = $1
      sub(/\{.*/, "", name)
      value = $NF + 0
    }
    name == "http_requests_received_total" {
      code = "other"
      if (match($0, /code="[0-9]+"/)) code = substr($0, RSTART + 6, RLENGTH - 7)
      requests[code] += value
      total += value
      if (code ~ /^5/) errors += value
    }
    name == "http_request_duration_seconds_sum" { duration_sum += value }
    name == "http_request_duration_seconds_count" { duration_count += value }
    name == "http_requests_in_progress" { in_progress += value }
    name == "process_start_time_seconds" { started = value }
    name == "process_working_set_bytes" { working_set = value }
    name == "dotnet_total_memory_bytes" { heap = value }
    name == "process_cpu_seconds_total" { cpu = value }
    name == "process_num_threads" { threads = value }
    END {
      n = 0
      for (code in requests) codes[++n] = code
      for (i = 2; i <= n; i++) for (j = i; j > 1 && codes[j] < codes[j - 1]; j--) { t = codes[j]; codes[j] = codes[j - 1]; codes[j - 1] = t }
      by_code = ""
      for (i = 1; i <= n; i++) by_code = by_code sprintf("%s=%d ", codes[i], requests[codes[i]])
      if (started > 0) printf "  %-16s %s\n", "Uptime", duration(now - started)
      printf "  %-16s %d (%s)\n", "Requests", total, (by_code == "" ? "none" : by_code)
      if (duration_count > 0) printf "  %-16s %.0f ms\n", "Avg duration", duration_sum / duration_count * 1000
      printf "  %-16s %d\n", "In progress", in_progress
      printf "  %-16s %.0f MiB (managed heap %.0f MiB)\n", "Working set", working_set / 1048576, heap / 1048576
      printf "  %-16s %.1f s, %d threads\n", "CPU time", cpu, threads
      if (errors > 0) printf "WARN\t%s answered %d request(s) with 5xx since it started\n", pod, errors
    }
  '
}

section_app() {
  section "Application health and metrics"
  ensure_kube || return
  local services="/api/v1/namespaces/$NAMESPACE/services" answer pod pods metrics

  sub "Health endpoints (through the Kubernetes API proxy)"
  if answer="$(kube get --raw "$services/$BACKEND_DEPLOYMENT:http/proxy/health/ready" 2>&1)"; then
    ok "Backend /health/ready (includes the database check): $answer"
  else
    fail "Backend /health/ready failed, so the database or the backend is down: $(last_line "$answer")"
  fi
  if answer="$(kube get --raw "$services/$FRONTEND_DEPLOYMENT:http/proxy/health" 2>&1)"; then
    ok "Frontend /health: $answer"
  else
    fail "Frontend /health failed: $(last_line "$answer")"
  fi

  sub "Backend metrics per pod (port $BACKEND_METRICS_PORT; counters since the pod started, probes included)"
  if ! pods="$(
    kube get pods -n "$NAMESPACE" -l "app.kubernetes.io/name=$BACKEND_DEPLOYMENT" \
      --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}' 2>&1
  )"; then
    warn "Could not list the backend pods to read metrics from: $(last_line "$pods")"
    return
  fi
  if [ -z "$pods" ]; then
    warn "No running backend pods to read metrics from"
    return
  fi
  for pod in $pods; do
    if ! metrics="$(kube get --raw "/api/v1/namespaces/$NAMESPACE/pods/$pod:$BACKEND_METRICS_PORT/proxy/metrics" 2>&1)"; then
      warn "Could not read metrics from $pod: $(last_line "$metrics")"
      continue
    fi
    printf '%s\n' "$pod"
    relay < <(metrics_summary "$pod" <<<"$metrics")
  done
}

section_events() {
  section "Warning events"
  ensure_kube || return
  local namespace events count

  for namespace in "$NAMESPACE" kube-system; do
    sub "$namespace"
    if ! events="$(
      kube get events -n "$namespace" --field-selector type=Warning --sort-by=.lastTimestamp \
        -o custom-columns='LAST:.lastTimestamp,COUNT:.count,REASON:.reason,OBJECT:.involvedObject.name,MESSAGE:.message' 2>&1
    )"; then
      warn "Could not read the events in $namespace: $(last_line "$events")"
      continue
    fi
    if [ -z "$events" ] || [[ "$events" == "No resources found"* ]]; then
      ok "No warning events in $namespace"
      continue
    fi
    count=$(($(grep -c . <<<"$events") - 1))
    head -n 1 <<<"$events"
    tail -n +2 <<<"$events" | tail -n 25 | cut -c1-240
    warn "$count warning event(s) in $namespace (Kubernetes keeps events for about an hour)"
  done
}

section_logs() {
  section "Application logs (last $LOG_TAIL_LINES lines per pod)"
  ensure_kube || return
  local deployment logs errors name restarts restarted=0

  for deployment in "$BACKEND_DEPLOYMENT" "$FRONTEND_DEPLOYMENT"; do
    sub "$deployment"
    if ! logs="$(
      kube logs -n "$NAMESPACE" -l "app.kubernetes.io/name=$deployment" \
        --all-containers --prefix --tail="$LOG_TAIL_LINES" --max-log-requests=10 2>&1
    )"; then
      warn "Could not read $deployment logs: $(last_line "$logs")"
      continue
    fi
    if [[ "$logs" == "No resources found"* ]]; then
      warn "No $deployment pods to read logs from"
      continue
    fi
    if [ -z "$logs" ]; then
      note "(no log lines)"
      continue
    fi
    printf '%s\n' "$logs"
    errors="$(grep -ciE '\b(fail|error|crit|critical|exception|panic|emerg)\b' <<<"$logs" || true)"
    if [ "$errors" -gt 0 ]; then
      warn "$deployment: $errors of the recent log lines mention errors or exceptions"
    else
      ok "$deployment: no errors in the recent log lines"
    fi
  done

  sub "Logs from before the last restart"
  if ! restarts="$(
    kube get pods -n "$NAMESPACE" \
      -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[*].restartCount}{"\n"}{end}' 2>&1
  )"; then
    warn "Could not read the container restart counts: $(last_line "$restarts")"
    return
  fi
  for name in $(awk '{ total = 0; for (i = 2; i <= NF; i++) total += $i; if (total > 0) print $1 }' <<<"$restarts"); do
    restarted=$((restarted + 1))
    printf '%s\n' "$name"
    kube logs -n "$NAMESPACE" "$name" --all-containers --previous --prefix --tail=50 2>&1 | sed 's/^/  /'
  done
  if [ "$restarted" -eq 0 ]; then
    note "No restarted containers."
  fi
}

section_control_plane() {
  section "EKS control plane logs (last ${LOOKBACK_HOURS}h)"
  local group="/aws/eks/$EKS_CLUSTER_NAME/cluster" info retention bytes types spec prefix pattern events count

  info="$(
    awsr logs describe-log-groups --log-group-name-prefix "$group" \
      --query "logGroups[?logGroupName=='$group'].[retentionInDays, storedBytes]" \
      --output text 2>&1
  )" || {
    fail "Could not read log group $group: $(last_line "$info")"
    return
  }
  if [ -z "$info" ]; then
    warn "Log group $group does not exist"
    return
  fi
  read -r retention bytes <<<"$info"
  types="$(
    awsr eks describe-cluster --name "$EKS_CLUSTER_NAME" \
      --query 'cluster.logging.clusterLogging[?enabled].types[]' --output text 2>/dev/null
  )" || types="unknown (describe-cluster failed)"
  printf '%-10s %s\n' "Group" "$group" "Retention" "$retention days" "Stored" "$(awk -v bytes="$bytes" 'BEGIN { printf "%.1f MiB", bytes / 1048576 }')" "Types" "$(tr '\t' ' ' <<<"${types:-none}")"

  for spec in "authenticator|\"access denied\"" "kube-controller-manager|?error ?Error ?failed ?Failed" "kube-scheduler|?error ?Error ?failed ?Failed"; do
    prefix="${spec%%|*}"
    pattern="${spec#*|}"
    sub "$prefix: $pattern"
    events="$(
      awsr logs filter-log-events \
        --log-group-name "$group" \
        --log-stream-name-prefix "$prefix" \
        --start-time $((WINDOW_START * 1000)) \
        --filter-pattern "$pattern" \
        --max-items 1000 \
        --query 'events[].[message]' \
        --output text 2>&1
    )" || {
      warn "Could not search $prefix logs: $(last_line "$events")"
      continue
    }
    events="$(grep -v '^NEXTTOKEN' <<<"$events" || true)"
    count="$(grep -c . <<<"$events" || true)"
    if [ "$count" -eq 0 ]; then
      ok "$prefix: nothing matching"
    else
      tail -n 10 <<<"$events" | cut -c1-300
      warn "$prefix: $count matching line(s)"
    fi
  done
}

section_ecr() {
  section "Container images (ECR)"
  local pair url deployment repo images newest running tag row scan critical high compare=true
  ensure_kube || compare=false

  for pair in "$BACKEND_REPOSITORY_URL|$BACKEND_DEPLOYMENT" "$FRONTEND_REPOSITORY_URL|$FRONTEND_DEPLOYMENT"; do
    url="${pair%%|*}"
    deployment="${pair#*|}"
    if [ -z "$url" ]; then
      skip "No repository URL for $deployment"
      continue
    fi
    repo="${url##*/}"
    sub "$repo (newest first)"
    images="$(
      awsr ecr describe-images --repository-name "$repo" \
        --query 'reverse(sort_by(imageDetails, &imagePushedAt))[:5].[imagePushedAt, imageScanStatus.status || `NONE`, imageScanFindingsSummary.findingSeverityCounts.CRITICAL || `0`, imageScanFindingsSummary.findingSeverityCounts.HIGH || `0`, join(`,`, imageTags || `[]`)]' \
        --output text 2>&1
    )" || {
      fail "Could not list images in $repo: $(last_line "$images")"
      continue
    }
    if [ -z "$images" ]; then
      fail "$repo has no images"
      continue
    fi
    printf '%-20s %-10s %-9s %-5s %s\n' "PUSHED" "SCAN" "CRITICAL" "HIGH" "TAGS"
    awk -F'\t' '{ printf "%-20s %-10s %-9s %-5s %s\n", substr($1, 1, 19), $2, $3, $4, $5 }' <<<"$images"
    newest="$(head -n 1 <<<"$images" | cut -f5)"

    if [ "$compare" = false ]; then
      note "Not connected to the cluster, so the running image isn't compared."
      continue
    fi
    if ! running="$(
      kube get deployment "$deployment" -n "$NAMESPACE" \
        -o jsonpath='{.spec.template.spec.containers[0].image}' 2>&1
    )"; then
      warn "Could not read the image $deployment runs: $(last_line "$running")"
      continue
    fi
    tag="${running##*:}"
    if [ -z "$running" ]; then
      warn "Deployment $deployment has no container image"
      continue
    elif [[ ",$newest," == *",$tag,"* ]]; then
      ok "$deployment runs the newest image ($tag)"
    else
      warn "$deployment runs $tag but the newest $repo image is tagged ${newest:-untagged}"
    fi
    if ! row="$(
      awsr ecr describe-images --repository-name "$repo" --image-ids "imageTag=$tag" \
        --query 'imageDetails[0].[imageScanStatus.status || `NONE`, imageScanFindingsSummary.findingSeverityCounts.CRITICAL || `0`, imageScanFindingsSummary.findingSeverityCounts.HIGH || `0`]' \
        --output text 2>&1
    )"; then
      warn "Could not read the scan results for $deployment image $tag: $(last_line "$row")"
      continue
    fi
    read -r scan critical high <<<"$row"
    if [ "${critical:-0}" -gt 0 ] || [ "${high:-0}" -gt 0 ]; then
      warn "Running $deployment image $tag has $critical critical and $high high scan findings"
    elif [ -n "$scan" ]; then
      ok "Running $deployment image $tag: scan $scan, no critical or high findings"
    fi
  done
}

section_cicd() {
  section "Deploy pipeline"
  local doc commands latest requested status tag id output workflow run run_status conclusion created sha title
  local expected actual environment_variables repository_variables key value want have mismatches=0 missing=0

  sub "SSM deploy commands ($DEPLOY_SSM_DOCUMENT)"
  if ! doc="$(awsr ssm describe-document --name "$DEPLOY_SSM_DOCUMENT" --query 'Document.Status' --output text 2>&1)"; then
    fail "Could not read deploy document $DEPLOY_SSM_DOCUMENT: $(last_line "$doc")"
  elif [ "$doc" = Active ]; then
    ok "Deploy document $DEPLOY_SSM_DOCUMENT is Active"
  else
    fail "Deploy document $DEPLOY_SSM_DOCUMENT is ${doc:-missing}"
  fi
  if ! commands="$(
    awsr ssm list-commands --filters "key=DocumentName,value=$DEPLOY_SSM_DOCUMENT" \
      --query 'Commands[].[RequestedDateTime, Status, Parameters.ImageTag[0], CommandId]' \
      --output text 2>&1
  )"; then
    fail "Could not list the deploy commands: $(last_line "$commands")"
  elif [ -z "$commands" ]; then
    note "No deploy commands in the last 30 days."
  else
    commands="$(sort -r <<<"$commands" | head -n 5)"
    awk -F'\t' '{ printf "%-20s %-10s %s\n", substr($1, 1, 19), $2, $3 }' <<<"$commands"
    IFS=$'\t' read -r requested status tag id <<<"$(head -n 1 <<<"$commands")"
    case "$status" in
      Success) ok "Last deploy ($tag) succeeded at ${requested%%.*}" ;;
      Pending | InProgress | Delayed) note "Deploy of $tag is $status." ;;
      *)
        fail "Last deploy ($tag) ended as $status"
        output="$(
          awsr ssm list-command-invocations --command-id "$id" --details \
            --query 'CommandInvocations[0].CommandPlugins[0].Output' --output text 2>/dev/null || true
        )"
        tail -n 15 <<<"$output" | sed 's/^/    /'
        ;;
    esac
  fi

  sub "GitHub Actions ($GITHUB_REPOSITORY)"
  if ! has_command gh; then
    skip "gh is not installed"
    return
  fi
  if ! gh auth status >/dev/null 2>&1; then
    skip "gh is not logged in (gh auth login)"
    return
  fi
  gh run list -R "$GITHUB_REPOSITORY" -L 10 || warn "Could not list workflow runs"
  while IFS= read -r workflow; do
    [ -z "$workflow" ] && continue
    if ! run="$(
      gh run list -R "$GITHUB_REPOSITORY" -w "$workflow" -L 1 \
        --json status,conclusion,createdAt,headSha,displayTitle \
        -q '.[] | [.status, .conclusion, .createdAt, .headSha[0:7], .displayTitle] | @tsv' 2>&1
    )"; then
      warn "Could not read the latest $workflow run: $(last_line "$run")"
      continue
    fi
    if [ -z "$run" ]; then
      note "$workflow: no runs"
      continue
    fi
    IFS=$'\t' read -r run_status conclusion created sha title <<<"$run"
    if [ "$run_status" != completed ]; then
      note "$workflow is $run_status for $sha ($title)."
    elif [ "$conclusion" = success ] || [ "$conclusion" = skipped ]; then
      ok "$workflow: $conclusion for $sha at $created"
    else
      fail "$workflow: $conclusion for $sha at $created ($title)"
    fi
  done < <(sed -n 's/^name:[[:space:]]*//p' "$REPO_ROOT"/CICD/.github/workflows/*.yml 2>/dev/null)

  sub "Repository variables vs Terraform outputs"
  if ! has_command terraform || ! has_command jq; then
    skip "terraform and jq are needed to compare the repository variables"
    return
  fi
  if ! expected="$(
    terraform -chdir="$REPO_ROOT/terraform" output -json github_actions_variables 2>/dev/null \
      | jq -r 'to_entries[] | [.key, (.value | tostring)] | @tsv'
  )"; then
    skip "Terraform state has no github_actions_variables output"
    return
  fi
  if ! environment_variables="$(
    gh variable list -R "$GITHUB_REPOSITORY" --env "$GITHUB_DEPLOY_ENVIRONMENT" \
      --json name,value -q '.[] | [.name, .value] | @tsv' 2>&1
  )"; then
    warn "Could not list the $GITHUB_DEPLOY_ENVIRONMENT environment variables: $(last_line "$environment_variables")"
    return
  fi
  if ! repository_variables="$(
    gh variable list -R "$GITHUB_REPOSITORY" \
      --json name,value -q '.[] | [.name, .value] | @tsv' 2>&1
  )"; then
    warn "Could not list the repository variables: $(last_line "$repository_variables")"
    return
  fi
  actual="$environment_variables"$'\n'"$repository_variables"
  while IFS=$'\t' read -r key want; do
    [ -z "$key" ] && continue
    have="$(awk -F'\t' -v key="$key" '$1 == key { print $2; exit }' <<<"$actual")"
    if [ -z "$have" ]; then
      missing=$((missing + 1))
      warn "$key is not a repository or $GITHUB_DEPLOY_ENVIRONMENT variable (fine if it is a secret); Terraform expects $want"
    elif [ "$have" != "$want" ]; then
      mismatches=$((mismatches + 1))
      fail "$key is $have but Terraform expects $want"
    fi
  done <<<"$expected"
  if [ "$mismatches" -eq 0 ] && [ "$missing" -eq 0 ]; then
    ok "GitHub variables match the Terraform outputs"
  fi
}

section_cost() {
  section "Month-to-date spend (credits and refunds excluded)"
  local start end rows total
  start="$(date -u +%Y-%m-01)"
  end="$(date -u -d tomorrow +%Y-%m-%d)"
  rows="$(
    aws ce get-cost-and-usage \
      --time-period "Start=$start,End=$end" \
      --granularity MONTHLY \
      --metrics UnblendedCost \
      --group-by Type=DIMENSION,Key=SERVICE \
      --filter '{"Not":{"Dimensions":{"Key":"RECORD_TYPE","Values":["Credit","Refund"]}}}' \
      --query 'ResultsByTime[].Groups[].[Keys[0], Metrics.UnblendedCost.Amount]' \
      --output text 2>&1
  )" || {
    fail "Cost Explorer query failed: $(last_line "$rows")"
    return
  }
  sort -t $'\t' -k2 -gr <<<"$rows" | awk -F'\t' '$2 >= 0.005 { printf "%-52s $%9.2f\n", $1, $2 }'
  total="$(awk -F'\t' '{ total += $2 } END { printf "%.2f", total }' <<<"$rows")"
  printf '%-52s $%9s\n' "Total" "$total"
  ok "Spend from $start to today: \$$total before credits"
  note "Cost Explorer bills \$0.01 per request; this section makes one."
}

summary() {
  section "Summary"
  local entry status name message oks=0 warns=0 fails=0 skips=0
  for entry in "${RESULTS[@]}"; do
    case "${entry%%|*}" in
      OK) oks=$((oks + 1)) ;;
      WARN) warns=$((warns + 1)) ;;
      FAIL) fails=$((fails + 1)) ;;
      SKIP) skips=$((skips + 1)) ;;
    esac
  done
  printf '%s%d ok%s, %s%d warning(s)%s, %s%d failure(s)%s, %d skipped\n' \
    "$GREEN" "$oks" "$RESET" "$YELLOW" "$warns" "$RESET" "$RED" "$fails" "$RESET" "$skips"
  for entry in "${RESULTS[@]}"; do
    status="${entry%%|*}"
    [ "$status" = OK ] && continue
    name="${entry#*|}"
    message="${name#*|}"
    name="${name%%|*}"
    case "$status" in
      FAIL) printf '%s%-4s%s %-14s %s\n' "$RED" "$status" "$RESET" "$name" "$message" ;;
      WARN) printf '%s%-4s%s %-14s %s\n' "$YELLOW" "$status" "$RESET" "$name" "$message" ;;
      *) printf '%s%-4s%s %-14s %s\n' "$DIM" "$status" "$RESET" "$name" "$message" ;;
    esac
  done
  [ "$fails" -eq 0 ]
}

require_command aws
if ! identity="$(aws sts get-caller-identity --query '[Account, Arn]' --output text 2>/dev/null)"; then
  printf 'No usable AWS credentials. Run: aws login\n' >&2
  exit 1
fi

section "HospitalSystem monitoring"
printf '%-10s %s\n' \
  "Time" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" \
  "Account" "${identity%%$'\t'*}" \
  "Caller" "${identity#*$'\t'}" \
  "Region" "$AWS_REGION" \
  "Cluster" "$EKS_CLUSTER_NAME" \
  "Namespace" "$NAMESPACE" \
  "App" "$APP_URL" \
  "Window" "last ${LOOKBACK_HOURS}h" \
  "Sections" "${SELECTED[*]}"

for CURRENT_SECTION in "${SELECTED[@]}"; do
  "section_${CURRENT_SECTION//-/_}" || true
done

CURRENT_SECTION=summary
summary
