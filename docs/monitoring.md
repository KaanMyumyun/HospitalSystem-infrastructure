# Monitoring

Monitoring is deliberately light. The project runs no Prometheus, Grafana,
CloudWatch Observability or EKS runtime monitoring, which would cost money to
keep running. Instead, `scripts/monitoring.sh` checks everything on demand, and
three kinds of CloudWatch alarm email you when something breaks.

## Checking the stack

```bash
./scripts/monitoring.sh                    # everything; changes nothing in AWS
./scripts/monitoring.sh --refresh-alarms   # also recreate the ALB alarms (monitoring.yml) first
./scripts/monitoring.sh workloads logs     # only the named sections
```

It ends with a summary of warnings and failures, and exits 1 when any check
fails. A check that can't read its data, because an AWS, Kubernetes, Helm or
GitHub call fails, is reported as a failure with the error, or as a warning
for checks that only ever warn. It never counts as healthy.
`./scripts/monitoring.sh --help` lists the sections and the environment
overrides (`LOOKBACK_HOURS`, `LOG_TAIL_LINES`, and others).

| Area | What it checks |
| ---- | -------------- |
| Public endpoint | DNS target, redirect, HTTPS, TLS and ACM expiry |
| Alarms | CloudWatch alarm states and recent changes |
| Load balancer | ALB metrics and target health |
| EKS | The control plane, upgrade insights, the node group and ops instance, nodes and kube-system add-ons |
| Workloads | Rollouts, HPAs, usage against limits, warning events and app logs |
| Backend | The `/health/ready` database check, and its Prometheus metrics on port 9091, read through the Kubernetes API proxy |
| Control plane logs | Errors |
| Releases | ECR scan findings, the SSM deploy history, GitHub Actions runs, and whether the repository variables still match the Terraform outputs |
| Cost | Month-to-date spend without credits: one Cost Explorer call, $0.01 per run |

The most common checks, and the same Kubernetes metrics queried directly:

```bash
./scripts/monitoring.sh alarms workloads   # alarm states, HPA targets, pod CPU/memory from metrics-server
./scripts/monitoring.sh logs               # recent app logs
kubectl get hpa -n hospitalsystem
kubectl top pods -n hospitalsystem
```

## Checking a rollout

`scripts/rollout-check.sh` answers a narrower question: does a deploy really
replace the pods without dropping a request? It also exits 1 when any check
fails.

```bash
./scripts/rollout-check.sh             # settings only; changes nothing
./scripts/rollout-check.sh --watch     # probes the app while a CI deploy rolls out
./scripts/rollout-check.sh --restart   # starts a rollout itself and probes it
```

- **No options:** reads the `RollingUpdate` settings on both Deployments, the
  ALB readiness gate on the namespace and the running pods, the Load Balancer
  Controller's pod webhook, the target group drain time in the Ingress and in
  the ALB itself, and whether the nodes have room for the extra pod a rollout
  starts.
- **`--watch`:** start it before triggering the deploy workflow. It waits for a
  rollout to begin, then samples both Deployments and requests the frontend
  and the backend about once a second until the rollout finishes.
- **`--restart`:** starts the rollout itself with `kubectl rollout restart`,
  which replaces the running pods with the same image, and probes it the same
  way.

While probing, it fails if a request is refused or answers 5xx, if a Deployment
ever drops to zero ready pods, or if the Deployments couldn't be read in any
sample, since their ready pods are then unknown. It reports whether it saw the
extra pod start before the old one stopped, and stops watching after three
samples in a row without the Deployment status.

## Alarms

The alarms are built from existing AWS metrics:

| Alarm | Created by | Fires when |
| ----- | ---------- | ---------- |
| `hospitalsystem-alb-5xx` | `ansible/playbooks/monitoring.yml` | The ALB returns HTTP 5xx responses |
| `hospitalsystem-unhealthy-targets-*`, one per target group | `ansible/playbooks/monitoring.yml` | A target group has unhealthy targets |
| `hospitalsystem-nodegroup-missing-nodes` | Terraform (`terraform/monitoring.tf`) | The EKS node group has fewer in-service nodes than desired for 15 minutes |

- **The ALB alarms** come from Ansible because the ALB only exists once the Load
  Balancer Controller has made it. Create or refresh them with
  `ansible-playbook ansible/playbooks/monitoring.yml`. They aren't in
  Terraform, so the cleanup deletes them with the ALB:
  `scripts/pre-destroy-cleanup.sh --apply` and the `cleanup-kubernetes.yml`
  playbook Terraform runs during destroy both delete `hospitalsystem-alb-5xx`
  and `hospitalsystem-unhealthy-targets-*`, and never Terraform's own alarm.
- **The node group alarm** stays OK when you scale the node group to 0 on
  purpose. It reads the Auto Scaling group's group metrics, which EKS leaves
  off; Terraform turns on the two it needs (free at one-minute granularity).
  Without them the alarm shows `INSUFFICIENT_DATA` instead of a false OK.

### Alert email

Every alarm emails the SNS topic `hospitalsystem-alerts` when it goes into
ALARM and when it returns to OK, which also happens once as each new alarm
first gets data. The topic is encrypted with the project KMS key, whose policy
lets CloudWatch use it for that topic only.

The recipient is `ALERT_EMAIL=you@example.com` in `.env.local`;
`./scripts/tf.sh apply` requires it and subscribes it to the topic. **Click the
link in the "AWS Notification - Subscription Confirmation" email:** nothing is
delivered until you do. The topic is part of the environment, so every rebuild
needs a new confirmation; `./scripts/monitoring.sh alarms` warns while it's
pending.

To send a test email through the whole path:

```bash
aws cloudwatch set-alarm-state --alarm-name hospitalsystem-nodegroup-missing-nodes \
  --state-value ALARM --state-reason "Testing the alert email"
```

The alarm goes back to OK at its next evaluation, which sends the OK email.

## Resource sizing

The settings for this light demo profile:

| Setting | Backend | Frontend |
| ------- | ------- | -------- |
| CPU request | 100m | 50m |
| Memory request and limit | 512Mi | 128Mi |
| HPA replicas | two | two |

They were tested live with five minutes at 5 requests per second: 4.4 frontend
page and asset requests and 0.6 read-only demo API requests per second. All
1,500 requests returned HTTP 200.

- Steady CPU use was 12–60m per backend pod and about 1m per frontend pod;
  memory was 73–90Mi and about 4Mi.
- The HPA minimums were still one then, and the backend HPA scaled to two once
  CPU passed its target.
- With both apps held at two replicas, concurrent rolling restarts finished
  under traffic with no container restarts or node memory pressure. One
  replacement frontend pod waited nine seconds for memory to free while an old
  backend pod drained, then scheduled.

The test didn't measure maximum throughput, or size workloads that write
appointments or hash passwords. Draining pods can use capacity
[beyond the replica count plus surge](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#updating-a-deployment),
so account for per-node placement and system pods before raising replica caps
or resource limits. The node and database sizes are Terraform variables (see
[Layout and sizing](terraform.md#layout-and-sizing)).

## Debug Deployed Images

For a quick answer, `./scripts/image-check.sh` (or `backend` / `frontend` for
one app) shows each app's image tag, whether every pod runs the digest that tag
points to in ECR, and the commit it was built from compared with main on
GitHub. It changes nothing. `./scripts/monitoring.sh ecr` runs the same check
next to the ECR image list and scan results.

When a deploy breaks and you need to see exactly what's running, use `kubectl`
with the tunnel kubeconfig from [Cluster access](operations.md#cluster-access):
run `export KUBECONFIG="$PWD/.generated/kubeconfig"` first.

The image configured on each Deployment, and the one each running pod uses:

```bash
kubectl get deployment hospital-backend hospital-frontend \
  -n hospitalsystem \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'

kubectl get pods -n hospitalsystem \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[0].image}{"\n"}{end}'
```

Rollout status and history, and the image stored in one revision:

```bash
kubectl rollout status deployment/hospital-backend -n hospitalsystem
kubectl rollout status deployment/hospital-frontend -n hospitalsystem

kubectl rollout history deployment/hospital-backend -n hospitalsystem
kubectl rollout history deployment/hospital-frontend -n hospitalsystem

kubectl rollout history deployment/hospital-backend -n hospitalsystem --revision=2
kubectl rollout history deployment/hospital-frontend -n hospitalsystem --revision=2
```

Roll back to the previous revision (each Deployment keeps five):

```bash
kubectl rollout undo deployment/hospital-backend -n hospitalsystem
kubectl rollout undo deployment/hospital-frontend -n hospitalsystem
```

Why pods fail:

```bash
kubectl get pods -n hospitalsystem
kubectl describe deployment hospital-backend -n hospitalsystem
kubectl describe deployment hospital-frontend -n hospitalsystem
kubectl describe pod -n hospitalsystem <pod-name>
kubectl logs -n hospitalsystem <pod-name>
```

A release tag reads `<date>-<short SHA>-<run number>-<attempt>`, so in
`2026-08-22-8f88481-57-1` the Git short SHA is `8f88481`; bootstrap tags stop
after the SHA, or after `-dirty`. To see the matching application commit:

```bash
cd ../HospitalSystem
git show --stat 8f88481
```
