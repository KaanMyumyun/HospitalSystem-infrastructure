# Monitoring

Monitoring is intentionally lightweight to avoid running a paid in-cluster
observability stack. The project does not deploy Prometheus, Grafana, CloudWatch
Observability, or EKS runtime monitoring.

`scripts/monitoring.sh` runs every check below in one pass and ends with a
summary of warnings and failures. It exits 1 when any check fails:

```bash
./scripts/monitoring.sh                    # everything; changes nothing in AWS
./scripts/monitoring.sh --refresh-alarms   # also recreate the ALB alarms (monitoring.yml) first
./scripts/monitoring.sh workloads logs     # only the named sections
```

It covers the public endpoint (DNS target, redirect, HTTPS, TLS and ACM expiry),
the CloudWatch alarm states and recent changes, ALB metrics and target health,
the EKS control plane, upgrade insights, node group and ops instance, nodes and
kube-system add-ons, workloads, rollouts, HPAs, usage against limits, the
backend's `/health/ready` database check and its Prometheus metrics on port
9091 (read through the Kubernetes API proxy), warning events, app logs, control
plane log errors, ECR scan findings, the SSM deploy history, GitHub Actions
runs, whether the repository variables still match the Terraform outputs, and
month-to-date spend without credits (one Cost Explorer call, $0.01 per run). Run
`./scripts/monitoring.sh --help` for the section list and the environment
overrides (`LOOKBACK_HOURS`, `LOG_TAIL_LINES`, and others).

When a check can't read its data (an AWS, Kubernetes, Helm or GitHub call
fails), `scripts/monitoring.sh` reports it as a failure, or as a warning for
checks that only ever warn, with the error. It never counts an unreadable check
as healthy.

`scripts/rollout-check.sh` answers a narrower question: whether a deploy really
replaces the pods without dropping a request. It also exits 1 when any check
fails:

```bash
./scripts/rollout-check.sh             # settings only; changes nothing
./scripts/rollout-check.sh --watch     # probes the app while a CI deploy rolls out
./scripts/rollout-check.sh --restart   # starts a rollout itself and probes it
```

With no options it reads the `RollingUpdate` settings on both Deployments, the
ALB readiness gate on the namespace and the running pods, the Load Balancer
Controller's pod webhook, the target group drain time in the Ingress and in the
ALB itself, and whether the nodes have room for the extra pod a rollout starts.

With `--watch` it waits for a rollout to begin, then samples both Deployments
and requests the frontend and the backend about once a second until the rollout
finishes. Start it before triggering the deploy workflow. `--restart` starts the
rollout itself with `kubectl rollout restart`, which replaces the running pods
with the same image. Either way it fails if a request is refused or answers 5xx,
if a Deployment ever drops to zero ready pods, or if the Deployments could not
be read in any sample (their ready pods are then unknown), and reports whether it
saw the extra pod start before the old one stopped. It stops watching after
three samples in a row without the Deployment status.

CloudWatch alarms are built from existing AWS metrics. The Ansible monitoring
playbook creates the ALB alarms, because the ALB only exists once the Load
Balancer Controller has made it:

- ALB HTTP 5xx responses
- unhealthy targets for each ALB target group

Terraform creates the node group alarm (`terraform/monitoring.tf`):

- EKS node group with fewer in-service nodes than desired for 15 minutes.
  Scaling the node group to 0 on purpose keeps it OK. It reads the Auto
  Scaling group's group metrics, which EKS leaves off; Terraform turns on the
  two it needs (free at one-minute granularity). Without them the alarm shows
  `INSUFFICIENT_DATA` instead of a false OK.

Every alarm emails the SNS topic `hospitalsystem-alerts` when it goes into
ALARM and when it returns to OK, which also happens once as each new alarm
first gets data. The topic is encrypted with the project KMS key, whose policy
lets CloudWatch use it for that topic only. Set the recipient in `.env.local`:

```bash
ALERT_EMAIL=you@example.com
```

`./scripts/tf.sh apply` requires it and subscribes it to the topic. AWS then
emails an "AWS Notification - Subscription Confirmation" link, and nothing is
delivered until you click it. The topic is part of the environment, so every
rebuild needs a new confirmation; `./scripts/monitoring.sh alarms` warns while
it's pending. To send a test email through the whole path:

```bash
aws cloudwatch set-alarm-state --alarm-name hospitalsystem-nodegroup-missing-nodes \
  --state-value ALARM --state-reason "Testing the alert email"
```

The alarm goes back to OK at its next evaluation, which sends the OK email.

The ALB alarms aren't in Terraform, so the cleanup deletes them with the ALB:
`scripts/pre-destroy-cleanup.sh --apply` and the `cleanup-kubernetes.yml`
playbook Terraform runs during destroy both delete `hospitalsystem-alb-5xx` and
`hospitalsystem-unhealthy-targets-*`, and never Terraform's own alarm.

Configure or refresh the ALB alarms:

```bash
ansible-playbook ansible/playbooks/monitoring.yml
```

Show current alarm states, Kubernetes runtime health (HPA targets and pod
CPU/memory usage from metrics-server included) and recent app logs:

```bash
./scripts/monitoring.sh alarms workloads
./scripts/monitoring.sh logs
```

You can query the same Kubernetes metrics directly:

```bash
kubectl get hpa -n hospitalsystem
kubectl top pods -n hospitalsystem
```

## Resource sizing

The resource settings were tested live on 2026-09-24 with five minutes at 5
requests per second: 4.4 frontend page and asset requests and 0.6 read-only
demo API requests per second. All 1,500 requests returned HTTP 200. Steady CPU
use was 12–60m per backend pod and about 1m per frontend pod; memory was
73–90Mi and about 4Mi. The HPA minimums were still one then, and the backend
HPA scaled to two once CPU passed its target. With both apps held at two
replicas, concurrent rolling restarts finished under traffic with no container
restarts or node memory pressure. One replacement frontend pod waited nine
seconds for memory to free while an old backend pod drained, then scheduled.

For this light demo profile, keep CPU requests at 100m (backend) and 50m
(frontend), memory requests and limits at 512Mi and 128Mi, and both HPAs at
two. The test didn't measure maximum throughput, or size workloads that write
appointments or hash passwords. Draining pods can use capacity
[beyond the replica count plus surge](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#updating-a-deployment),
so account for per-node placement and system pods before raising replica caps
or resource limits. The node and database sizes are Terraform variables (see
[Layout and sizing](terraform.md#layout-and-sizing)).

## Debug Deployed Images

For a quick answer, `./scripts/image-check.sh` (or `backend` / `frontend` for
one app) shows each app's image tag, whether every pod runs the digest that
tag points to in ECR, and the commit it was built from compared with main on
GitHub. It changes nothing; `./scripts/monitoring.sh ecr` runs the same check
next to the ECR image list and scan results.

Use these commands when a deployment breaks and you need to see exactly which
backend or frontend image is running. They use the tunnel kubeconfig from
[Cluster access](operations.md#cluster-access): run
`export KUBECONFIG="$PWD/.generated/kubeconfig"` first.

Show the image configured on each Deployment:

```bash
kubectl get deployment hospital-backend hospital-frontend \
  -n hospitalsystem \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'
```

Show the image used by each running Pod:

```bash
kubectl get pods -n hospitalsystem \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[0].image}{"\n"}{end}'
```

Check rollout status and revision history:

```bash
kubectl rollout status deployment/hospital-backend -n hospitalsystem
kubectl rollout status deployment/hospital-frontend -n hospitalsystem

kubectl rollout history deployment/hospital-backend -n hospitalsystem
kubectl rollout history deployment/hospital-frontend -n hospitalsystem
```

Inspect a specific rollout revision to see the image stored in that revision:

```bash
kubectl rollout history deployment/hospital-backend -n hospitalsystem --revision=2
kubectl rollout history deployment/hospital-frontend -n hospitalsystem --revision=2
```

Roll back to the previous revision (each Deployment keeps five):

```bash
kubectl rollout undo deployment/hospital-backend -n hospitalsystem
kubectl rollout undo deployment/hospital-frontend -n hospitalsystem
```

A release tag reads `<date>-<short SHA>-<run number>-<attempt>`, so in
`2026-08-22-8f88481-57-1` the Git short SHA is `8f88481`. Bootstrap tags stop
after the SHA, or after `-dirty`. Find the matching application commit from the
app repository:

```bash
cd ../HospitalSystem
git show --stat 8f88481
```

Useful failure checks:

```bash
kubectl get pods -n hospitalsystem
kubectl describe deployment hospital-backend -n hospitalsystem
kubectl describe deployment hospital-frontend -n hospitalsystem
kubectl describe pod -n hospitalsystem <pod-name>
kubectl logs -n hospitalsystem <pod-name>
```
