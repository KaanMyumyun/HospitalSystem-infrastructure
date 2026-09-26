#!/usr/bin/env bash
#./scripts/rollout-check.sh             # settings only; changes nothing
#./scripts/rollout-check.sh --watch     # probes the app while a CI deploy rolls out
#./scripts/rollout-check.sh --restart   # starts a rollout itself and probes it

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=scripts/lib/config.sh
source "$SCRIPT_DIR/lib/config.sh"

AWS_REGION="${AWS_REGION:-$(group_var aws_region)}"
NAMESPACE="${NAMESPACE:-$(group_var k8s_namespace)}"
BACKEND_DEPLOYMENT="${BACKEND_DEPLOYMENT:-$(group_var backend_deployment)}"
FRONTEND_DEPLOYMENT="${FRONTEND_DEPLOYMENT:-$(group_var frontend_deployment)}"
INGRESS_NAME="${INGRESS_NAME:-$(group_var ingress_name)}"
ALB_NAME="${ALB_NAME:-$(group_var alb_name)}"
APP_DOMAIN="${APP_DOMAIN:-$(group_var app_domain_name)}"
KUBE_TIMEOUT="${KUBE_TIMEOUT:-30s}"
PROBE_INTERVAL="${PROBE_INTERVAL:-1}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-3}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-600}"

MODE=settings

usage() {
  cat <<'USAGE'
Usage: scripts/rollout-check.sh [--watch | --restart] [--interval SECONDS] [--timeout SECONDS]

Checks whether the rolling deployment is set up correctly and, on request,
whether a real rollout serves every request. Exits 1 when any check fails.

With no options it only reads settings and changes nothing:
  - the RollingUpdate strategy, surge and shutdown settings on both Deployments
  - the ALB readiness gate on the namespace, the pods and the controller webhook
  - the target group drain time, in the Ingress and as the ALB has it
  - whether the nodes have room for the extra pod each rollout starts

Options:
  --watch           wait for a rollout to start (a CI deploy, or someone else's
                    kubectl), then probe the app until it finishes
  --restart         start a rollout with kubectl rollout restart and probe it;
                    this restarts the running pods with the same image
  --interval SECS   seconds between probes during a rollout (default 1)
  --timeout SECS    seconds to wait for the rollout (default 600)
  -h, --help        show this help

During a rollout the script records, for every sample: how many pods each
Deployment has, how many are ready, and the HTTP code from the frontend and
from the backend through the ALB. It fails if a request is refused or answers
5xx, if a Deployment ever drops to zero ready pods, or if the Deployments
could not be read in any sample, since their ready pods are then unknown.

Environment overrides: AWS_REGION, NAMESPACE, BACKEND_DEPLOYMENT,
FRONTEND_DEPLOYMENT, INGRESS_NAME, ALB_NAME, APP_DOMAIN, KUBE_TIMEOUT (30s),
PROBE_INTERVAL (1), PROBE_TIMEOUT (3), ROLLOUT_TIMEOUT (600), NO_COLOR.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --watch) MODE=watch ;;
    --restart) MODE=restart ;;
    --interval)
      [ $# -ge 2 ] || { printf -- '--interval needs a value\n' >&2; exit 1; }
      PROBE_INTERVAL="$2"
      shift
      ;;
    --timeout)
      [ $# -ge 2 ] || { printf -- '--timeout needs a value\n' >&2; exit 1; }
      ROLLOUT_TIMEOUT="$2"
      shift
      ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

for var in AWS_REGION NAMESPACE BACKEND_DEPLOYMENT FRONTEND_DEPLOYMENT INGRESS_NAME ALB_NAME APP_DOMAIN; do
  if [ -z "${!var}" ]; then
    printf 'Missing %s: export it, or run ./scripts/tf.sh apply to generate %s\n' \
      "$var" "$GROUP_VARS_DIR/terraform.yml" >&2
    exit 1
  fi
done

if ! [[ "$PROBE_INTERVAL" =~ ^[0-9]+(\.[0-9]+)?$ ]] || ! awk -v v="$PROBE_INTERVAL" 'BEGIN { exit !(v > 0) }'; then
  printf 'PROBE_INTERVAL must be a positive number, got: %s\n' "$PROBE_INTERVAL" >&2
  exit 1
fi

for var in PROBE_TIMEOUT ROLLOUT_TIMEOUT; do
  if ! [[ "${!var}" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s must be a positive whole number, got: %s\n' "$var" "${!var}" >&2
    exit 1
  fi
done

APP_URL="https://$APP_DOMAIN"
FRONTEND_URL="$APP_URL/health"
BACKEND_URL="$APP_URL/api/health"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\e[1m' DIM=$'\e[2m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RED=$'\e[31m' RESET=$'\e[0m'
else
  BOLD="" DIM="" GREEN="" YELLOW="" RED="" RESET=""
fi

export AWS_PAGER=""
# The playbooks write the tunnel kubeconfig here and leave ~/.kube/config alone.
export KUBECONFIG="$REPO_ROOT/.generated/kubeconfig"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

RESULTS=()
CURRENT_SECTION=""
KUBE_STATE=""

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

# Echoes HTTP code and content type, or 000 when the request never answered.
probe() {
  local answer
  answer="$(curl -sS -o /dev/null --max-time "$PROBE_TIMEOUT" -w '%{http_code}|%{content_type}' "$1" 2>/dev/null || true)"
  printf '%s' "${answer:-000|}"
}

# Echoes one line per Deployment: name|generation|observed|spec|pods|updated|ready|available
# A Deployment that could not be read gets name|unreadable|error instead, and
# the function returns 1, so a failed read never looks like 0 pods wanted.
deployment_state() {
  local deployment row status=0
  for deployment in "$BACKEND_DEPLOYMENT" "$FRONTEND_DEPLOYMENT"; do
    if ! row="$(
      kube get deployment "$deployment" -n "$NAMESPACE" -o jsonpath='{.metadata.generation}|{.status.observedGeneration}|{.spec.replicas}|{.status.replicas}|{.status.updatedReplicas}|{.status.readyReplicas}|{.status.availableReplicas}' 2>"$TMP_DIR/deployment.err"
    )"; then
      row="$(last_line "$(cat "$TMP_DIR/deployment.err")")"
      row="unreadable|${row:-kubectl get deployment failed}"
      status=1
    # The API server always sets generation and spec.replicas. The status
    # counts are left out while they are 0.
    elif ! [[ "$row" =~ ^[0-9]+\|[0-9]*\|[0-9]+(\|[0-9]*){4}$ ]]; then
      row="unreadable|unexpected kubectl output: $row"
      status=1
    fi
    printf '%s|%s\n' "$deployment" "$row"
  done
  return "$status"
}

# Echoes the first "deployment: error" from deployment_state output.
first_read_error() {
  printf '%s\n' "$1" | sed -n '/|unreadable|/{s//: /p;q;}'
}

section_settings() {
  section "Rolling update settings"
  ensure_kube || return
  local deployment row type surge unavailable min_ready deadline history grace sleep_for replicas

  for deployment in "$BACKEND_DEPLOYMENT" "$FRONTEND_DEPLOYMENT"; do
    sub "$deployment"
    row="$(
      kube get deployment "$deployment" -n "$NAMESPACE" -o jsonpath='{.spec.strategy.type}|{.spec.strategy.rollingUpdate.maxSurge}|{.spec.strategy.rollingUpdate.maxUnavailable}|{.spec.minReadySeconds}|{.spec.progressDeadlineSeconds}|{.spec.revisionHistoryLimit}|{.spec.template.spec.terminationGracePeriodSeconds}|{.spec.template.spec.containers[0].lifecycle.preStop.sleep.seconds}|{.spec.replicas}' 2>&1
    )" || {
      fail "Deployment $deployment not found: $(last_line "$row")"
      continue
    }
    IFS='|' read -r type surge unavailable min_ready deadline history grace sleep_for replicas <<<"$row"

    if [ "$type" = RollingUpdate ]; then
      ok "$deployment uses RollingUpdate"
    else
      fail "$deployment uses ${type:-an unset strategy}; every deploy takes the app down. Apply the manifests with ansible/playbooks/apply-kubernetes.yml"
      continue
    fi

    case "$unavailable" in
      0 | 0%) ok "$deployment keeps every replica up during a rollout (maxUnavailable $unavailable)" ;;
      "") warn "$deployment has no maxUnavailable; the default 25% rounds down to 0 for $replicas replica(s), but set it to 0 to be explicit" ;;
      *) fail "$deployment has maxUnavailable $unavailable, so a rollout may take the last pod down before the new one is ready" ;;
    esac

    case "$surge" in
      "" ) warn "$deployment has no maxSurge; the default 25% rounds up to 1 pod, but set it to be explicit" ;;
      0 | 0%) fail "$deployment has maxSurge 0, so no new pod can start before the old one stops" ;;
      *) ok "$deployment starts $surge extra pod(s) during a rollout (maxSurge $surge)" ;;
    esac

    if [ -z "$sleep_for" ]; then
      warn "$deployment has no preStop hook; a stopping pod can be killed while the ALB still sends it requests"
    elif [ -z "$grace" ]; then
      warn "$deployment waits ${sleep_for}s in preStop but has no terminationGracePeriodSeconds"
    elif [ "$grace" -le "$sleep_for" ]; then
      fail "$deployment waits ${sleep_for}s in preStop but is killed after ${grace}s, leaving no time to shut down"
    else
      ok "$deployment drains for ${sleep_for}s, then has $((grace - sleep_for))s to shut down"
    fi

    printf '%-28s %s\n' \
      "replicas" "$replicas" \
      "minReadySeconds" "${min_ready:-0}" \
      "progressDeadlineSeconds" "${deadline:-600}" \
      "revisionHistoryLimit" "${history:-10}"
  done
}

section_gates() {
  section "ALB readiness gate and drain time"
  ensure_kube || return
  local label webhook policy pods pod gates gate status annotation attributes tg tgs delay

  label="$(kube get namespace "$NAMESPACE" -o jsonpath='{.metadata.labels.elbv2\.k8s\.aws/pod-readiness-gate-inject}' 2>/dev/null || true)"
  if [ "$label" = enabled ]; then
    ok "Namespace $NAMESPACE asks the controller to add readiness gates"
  else
    fail "Namespace $NAMESPACE is missing elbv2.k8s.aws/pod-readiness-gate-inject=enabled, so a new pod counts as ready before the ALB sends it traffic"
  fi

  webhook=""
  for policy in $(kube get mutatingwebhookconfigurations -o name 2>/dev/null || true); do
    webhook="$(
      kube get "$policy" -o jsonpath='{range .webhooks[*]}{.name}={.failurePolicy}{"\n"}{end}' 2>/dev/null |
        awk -F= '$1 == "mpod.elbv2.k8s.aws" { print $2 }'
    )"
    [ -n "$webhook" ] && break
  done
  if [ -z "$webhook" ]; then
    warn "The Load Balancer Controller's pod webhook is not installed; run ansible/playbooks/load-balancer-controller.yml"
  elif [ "$webhook" = Ignore ]; then
    ok "Pod webhook failure policy is Ignore, so pods still start if the controller is down"
  else
    warn "Pod webhook failure policy is $webhook: pods cannot be created in labelled namespaces while the controller is down"
  fi

  sub "Pods"
  pods="$(kube get pods -n "$NAMESPACE" --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)"
  if [ -z "$pods" ]; then
    warn "No running pods in $NAMESPACE to check"
  fi
  for pod in $pods; do
    gates="$(kube get pod "$pod" -n "$NAMESPACE" -o jsonpath='{.spec.readinessGates[*].conditionType}' 2>/dev/null || true)"
    if [ -z "$gates" ]; then
      warn "$pod has no readiness gate; it was created before the namespace label or the Ingress existed. The next rollout fixes it"
      continue
    fi
    for gate in $gates; do
      status="$(kube get pod "$pod" -n "$NAMESPACE" -o jsonpath="{.status.conditions[?(@.type==\"$gate\")].status}" 2>/dev/null || true)"
      if [ "$status" = True ]; then
        ok "$pod passes its ALB target check (${gate##*/})"
      else
        warn "$pod gate ${gate##*/} is ${status:-missing}; the ALB has not called this pod healthy"
      fi
    done
  done

  sub "Drain time"
  annotation="$(kube get ingress "$INGRESS_NAME" -n "$NAMESPACE" -o jsonpath='{.metadata.annotations.alb\.ingress\.kubernetes\.io/target-group-attributes}' 2>/dev/null || true)"
  if [[ "$annotation" == *deregistration_delay.timeout_seconds* ]]; then
    ok "Ingress $INGRESS_NAME sets $annotation"
  else
    warn "Ingress $INGRESS_NAME does not set deregistration_delay.timeout_seconds, so target groups drain for the default 300s"
  fi

  tgs="$(
    awsr elbv2 describe-target-groups --load-balancer-arn "$(
      awsr elbv2 describe-load-balancers --names "$ALB_NAME" --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null
    )" --query 'TargetGroups[].TargetGroupArn' --output text 2>/dev/null || true
  )"
  if [ -z "$tgs" ] || [ "$tgs" = None ]; then
    skip "No target groups found for $ALB_NAME"
    return
  fi
  for tg in $tgs; do
    attributes="$(
      awsr elbv2 describe-target-group-attributes --target-group-arn "$tg" \
        --query "Attributes[?Key=='deregistration_delay.timeout_seconds'].Value" --output text 2>/dev/null || true
    )"
    delay="${attributes:-unknown}"
    if ! [[ "$delay" =~ ^[0-9]+$ ]]; then
      warn "Could not read the drain time of ${tg##*/}"
    elif [ "$delay" -le 60 ]; then
      ok "Target group ${tg##*/} drains for ${delay}s"
    else
      warn "Target group ${tg##*/} drains for ${delay}s; a stopped pod stays registered that long"
    fi
  done
}

section_capacity() {
  section "Room for the extra pod"
  ensure_kube || return
  local nodes node allocatable used free total_free=0 line pct

  nodes="$(kube get nodes --no-headers -o custom-columns='NAME:.metadata.name,PODS:.status.allocatable.pods' 2>/dev/null || true)"
  if [ -z "$nodes" ]; then
    fail "Could not list nodes"
    return
  fi

  if ! kube get pods -A --field-selector=status.phase=Running --no-headers \
    -o custom-columns='NODE:.spec.nodeName' >"$TMP_DIR/pod-nodes" 2>"$TMP_DIR/pod-nodes.err"; then
    fail "Could not list the running pods, so the free pod slots are unknown: $(last_line "$(cat "$TMP_DIR/pod-nodes.err")")"
    return
  fi
  sort "$TMP_DIR/pod-nodes" | uniq -c >"$TMP_DIR/pods-per-node"

  while read -r node allocatable; do
    used="$(awk -v node="$node" '$2 == node { print $1 }' "$TMP_DIR/pods-per-node")"
    used="${used:-0}"
    free=$((allocatable - used))
    total_free=$((total_free + free))
    printf '%-40s %s/%s pod slots used, %s free\n' "$node" "$used" "$allocatable" "$free"

    # "Allocated resources" prints the requests percentage first, then limits.
    if ! line="$(kube describe node "$node" 2>/dev/null | awk '/^Allocated resources:/ { inside = 1 } inside && ($1 == "cpu" || $1 == "memory") { print $1, $3 }')"; then
      warn "Could not read the resource requests on $node"
    fi
    while read -r resource pct; do
      [ -n "$resource" ] || continue
      pct="${pct//[()%]/}"
      printf '  %-8s %s%% requested\n' "$resource" "$pct"
      if [[ "$pct" =~ ^[0-9]+$ ]] && [ "$pct" -gt 85 ]; then
        warn "$node has $pct% of its $resource requested; the extra pod a rollout starts may not fit"
      fi
    done <<<"$line"
  done <<<"$nodes"

  # One rollout adds one backend pod and one frontend pod.
  if [ "$total_free" -ge 2 ]; then
    ok "$total_free free pod slots; a rollout needs 2 (one per Deployment)"
  else
    fail "Only $total_free free pod slot(s); a rollout needs 2 and the extra pods will stay Pending"
  fi
}

# Samples both Deployments and probes the app until the rollout finishes.
watch_rollout() {
  local deadline="$1" started_at samples=0 requests=0 failures=0 zero_ready=0 surge=0
  local line deployment generation observed spec pods updated ready
  local rolling sample_surge sample_zero sample_unreadable
  local unreadable=0 unreadable_in_a_row=0 first_unreadable="" scaled_down=""
  local frontend_probe backend_probe frontend_code backend_code backend_type
  local first_failure="" first_zero="" state summary_line

  started_at="$(date -u +%s)"
  printf '\n%-10s %-42s %-22s %s\n' "TIME" "PODS (have/ready of wanted)" "FRONTEND" "BACKEND"

  while true; do
    rolling=false
    state=""
    sample_surge=false
    sample_zero=false
    sample_unreadable=false
    while IFS= read -r line; do
      IFS='|' read -r deployment generation observed spec pods updated ready _ <<<"$line"
      if [ "$generation" = unreadable ]; then
        # Its ready pods are unknown and it may still be rolling.
        rolling=true
        sample_unreadable=true
        [ -n "$first_unreadable" ] || first_unreadable="$(first_read_error "$line")"
        state="$state$(printf '%s ?  ' "${deployment##*-}")"
        continue
      fi
      pods="${pods:-0}"
      ready="${ready:-0}"
      updated="${updated:-0}"
      state="$state$(printf '%s %s/%s of %s  ' "${deployment##*-}" "$pods" "$ready" "$spec")"

      if [ "$spec" = 0 ]; then
        [[ " $scaled_down " == *" $deployment "* ]] || scaled_down="$scaled_down $deployment"
        continue
      fi
      if [ "${observed:-0}" != "$generation" ] || [ "$updated" -lt "$spec" ] ||
        [ "$pods" -gt "$spec" ] || [ "$ready" -lt "$spec" ]; then
        rolling=true
      fi
      [ "$pods" -gt "$spec" ] && sample_surge=true
      if [ "$ready" -eq 0 ]; then
        sample_zero=true
        [ -n "$first_zero" ] || first_zero="$deployment"
      fi
    done < <(deployment_state)

    [ "$sample_surge" = true ] && surge=$((surge + 1))
    [ "$sample_zero" = true ] && zero_ready=$((zero_ready + 1))
    if [ "$sample_unreadable" = true ]; then
      unreadable=$((unreadable + 1))
      unreadable_in_a_row=$((unreadable_in_a_row + 1))
    else
      unreadable_in_a_row=0
    fi

    frontend_probe="$(probe "$FRONTEND_URL")"
    backend_probe="$(probe "$BACKEND_URL")"
    frontend_code="${frontend_probe%%|*}"
    backend_code="${backend_probe%%|*}"
    backend_type="${backend_probe#*|}"
    requests=$((requests + 2))
    samples=$((samples + 1))

    # The frontend must answer 200. /api/health is not a backend route, so any
    # answer from the app below 500 means the request reached the backend.
    if [ "$frontend_code" != 200 ]; then
      failures=$((failures + 1))
      [ -n "$first_failure" ] || first_failure="$FRONTEND_URL returned $frontend_code"
    fi
    if [ "$backend_code" = 000 ] || [ "${backend_code:-500}" -ge 500 ]; then
      failures=$((failures + 1))
      [ -n "$first_failure" ] || first_failure="$BACKEND_URL returned $backend_code"
    fi

    printf '%-10s %-42s %-22s %s\n' \
      "$(date -u +%H:%M:%S)" "$state" "$frontend_code" "$backend_code ${backend_type:-}"

    if [ "$rolling" = false ] && [ "$samples" -gt 1 ]; then
      break
    fi
    if [ "$unreadable_in_a_row" -ge 3 ]; then
      fail "Stopped watching: the Deployments could not be read in $unreadable_in_a_row samples in a row"
      break
    fi
    if [ "$(date -u +%s)" -ge "$deadline" ]; then
      fail "The rollout did not finish within ${ROLLOUT_TIMEOUT}s"
      break
    fi
    sleep "$PROBE_INTERVAL"
  done

  summary_line="$samples samples over $(($(date -u +%s) - started_at))s, $requests requests"
  if [ "$failures" -eq 0 ]; then
    ok "No request failed during the rollout ($summary_line)"
  else
    fail "$failures of $requests requests failed during the rollout; first: $first_failure ($summary_line)"
  fi

  if [ "$zero_ready" -gt 0 ]; then
    fail "$first_zero had 0 ready pods in $zero_ready sample(s); the rollout is not keeping the app up"
  fi
  if [ "$unreadable" -gt 0 ]; then
    fail "Could not read the Deployments in $unreadable of $samples sample(s), so their ready pods are unconfirmed; first error: $first_unreadable"
  fi
  if [ -n "$scaled_down" ]; then
    warn "Scaled to 0 replicas:$scaled_down; there were no pods to keep ready"
  fi
  if [ "$zero_ready" -eq 0 ] && [ "$unreadable" -eq 0 ] && [ -z "$scaled_down" ]; then
    ok "Both Deployments kept at least one ready pod the whole time"
  fi

  if [ "$surge" -gt 0 ]; then
    ok "Saw the extra pod start before the old one stopped in $surge sample(s), which is what RollingUpdate should do"
  else
    warn "Never saw more pods than replicas; the rollout may have been too fast to sample, or no rollout happened"
  fi
}

section_live() {
  section "Live rollout"
  ensure_kube || return
  local deadline deployment before after waiting_since unreadable_in_a_row=0

  if ! has_command curl; then
    fail "curl is needed to probe the app during a rollout"
    return
  fi

  deadline=$(($(date -u +%s) + ROLLOUT_TIMEOUT))

  if [ "$MODE" = restart ]; then
    note "Restarting both Deployments; this replaces the running pods with the same image."
    for deployment in "$BACKEND_DEPLOYMENT" "$FRONTEND_DEPLOYMENT"; do
      if ! run_quiet kube rollout restart "deployment/$deployment" -n "$NAMESPACE"; then
        fail "Could not restart $deployment"
        return
      fi
      ok "Restarted $deployment"
    done
  else
    note "Waiting up to ${ROLLOUT_TIMEOUT}s for a rollout to start. Trigger the deploy workflow now, or run ansible/playbooks/deploy-image.yml."
    if ! before="$(deployment_state)"; then
      fail "Could not read the Deployments: $(first_read_error "$before")"
      return
    fi
    waiting_since="$(date -u +%s)"
    while true; do
      # A failed read is not a change; only a readable new state starts the watch.
      if after="$(deployment_state)"; then
        unreadable_in_a_row=0
        if [ "$after" != "$before" ]; then
          ok "A rollout started after $(($(date -u +%s) - waiting_since))s"
          break
        fi
      else
        unreadable_in_a_row=$((unreadable_in_a_row + 1))
        if [ "$unreadable_in_a_row" -ge 3 ]; then
          fail "Stopped waiting for a rollout: the Deployments could not be read $unreadable_in_a_row times in a row; last error: $(first_read_error "$after")"
          return
        fi
      fi
      if [ "$(date -u +%s)" -ge "$deadline" ]; then
        skip "No rollout started within ${ROLLOUT_TIMEOUT}s"
        return
      fi
      sleep "$PROBE_INTERVAL"
    done
  fi

  watch_rollout "$deadline"
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
      FAIL) printf '%s%-4s%s %-10s %s\n' "$RED" "$status" "$RESET" "$name" "$message" ;;
      WARN) printf '%s%-4s%s %-10s %s\n' "$YELLOW" "$status" "$RESET" "$name" "$message" ;;
      *) printf '%s%-4s%s %-10s %s\n' "$DIM" "$status" "$RESET" "$name" "$message" ;;
    esac
  done
  [ "$fails" -eq 0 ]
}

require_command aws
require_command curl
if ! identity="$(aws sts get-caller-identity --query '[Account, Arn]' --output text 2>/dev/null)"; then
  printf 'No usable AWS credentials. Run: aws login\n' >&2
  exit 1
fi

section "HospitalSystem rollout check"
printf '%-10s %s\n' \
  "Time" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" \
  "Account" "${identity%%$'\t'*}" \
  "Region" "$AWS_REGION" \
  "Namespace" "$NAMESPACE" \
  "App" "$APP_URL" \
  "Mode" "$MODE"

SECTIONS=(settings gates capacity)
[ "$MODE" = settings ] || SECTIONS+=(live)

for CURRENT_SECTION in "${SECTIONS[@]}"; do
  "section_${CURRENT_SECTION}" || true
done

CURRENT_SECTION=summary
summary
