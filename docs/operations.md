# Operations

Day-to-day work on a running stack: reaching the cluster, the backend secret,
the playbooks, pausing, the database, costs, rotating the JWT key and
upgrading EKS.

## Ansible Operations

Ansible is used here as an operational wrapper around `aws eks` and `kubectl`.
It runs locally against the current AWS account and does not require SSH access
to the EKS nodes.

Requirements:

- `ansible-playbook`
- `aws`
- `kubectl`
- `helm`
- `session-manager-plugin`, because the EKS API endpoint is private (see
  [Cluster access](#cluster-access))
- AWS credentials that can access the `eks-pr1` cluster and start SSM sessions

Configuration values live in one place each:

- Terraform writes the values it owns (region, cluster name, namespace, deploy
  group, VPC ID, ECR repository URLs, IAM role ARNs, ACM certificate ARN,
  domains, alarm prefix, ops instance ID, database endpoint, name and user)
  to the ignored
  `ansible/group_vars/all/terraform.yml` on every apply.
- `ansible/group_vars/all/main.yml` holds the Ansible-owned settings: the
  Deployment, Ingress, and ALB names, the SSM tunnel port, Cloudflare and
  monitoring options, and the manifest list. The Kubernetes templates and
  `scripts/monitoring.sh` read these same values.

Run `./scripts/tf.sh apply` once on a machine before running playbooks by hand;
without the generated file they stop with a message saying so.

### Cluster access

The EKS API endpoint is private. Every playbook that talks to the cluster, and
`scripts/monitoring.sh`, goes through `ansible/tasks/kubeconfig.yml`. It opens
an SSM port-forwarding session through the ops instance to the endpoint and
points the kubeconfig cluster at `https://127.0.0.1:8443`
(`eks_tunnel_local_port`).

To set up the kubeconfig and open the tunnel without running anything else:

```bash
ansible-playbook ansible/playbooks/kubeconfig.yml
```

The playbooks write that kubeconfig to the ignored `.generated/kubeconfig`
(`kubeconfig_path`) and run `kubectl` and `helm` with `KUBECONFIG` pointing at
it, so `~/.kube/config` and your current context are never changed.
`scripts/monitoring.sh`, `scripts/rollout-check.sh` and
`scripts/pre-destroy-cleanup.sh` use the same file. For plain `kubectl` commands,
such as the ones in these docs, point your shell at it first:

```bash
export KUBECONFIG="$PWD/.generated/kubeconfig"
```

The session stays open for plain `kubectl` commands and closes after 12 hours.
Close it sooner, and always after rebuilding the cluster, since an open session
still points at the old endpoint:

```bash
pkill -f AWS-StartPortForwardingSessionToRemoteHost
```

The ops instance has no internet access: its security group only allows HTTPS
inside the VPC and to S3. It reaches Systems Manager through the `ssm`,
`ssmmessages` and `ec2messages` interface endpoints (in one AZ, roughly $25 a
month), and the Amazon Linux package repositories through the S3 gateway
endpoint. Without kubectl on the instance, the deploy document calls the
Kubernetes API with Python's standard library, using the cluster endpoint and CA
that Terraform writes into the document.

The ops instance's Kubernetes access is an EKS access entry in Terraform
(`aws_eks_access_entry.ops`). Bootstrap waits for it before applying the
deploy RBAC binding. There is no separate `aws-auth` playbook to run.

### Backend secret

The backend's connection string and JWT key live in the AWS Secrets Manager
secret `hospitalsystem/backend` (`terraform/secrets.tf`), encrypted with the
project's KMS key. Terraform creates the secret but never its value, so the
value stays out of Terraform state. `backend-secret.yml` builds the connection
string from the database endpoint Terraform reports and
`HOSPITALSYSTEM_DB_PASSWORD`, with `SSL Mode=Require` (RDS requires TLS) and
`Maximum Pool Size=20`. Each backend pod keeps its own pool, so pods × pool
size has to stay under what the database allows: `max_connections` is 79 on a
`db.t4g.micro`. With Npgsql's default of 100 per pod, the 2026-10-12 load test
made the database swap and fail at 160 requests/s. Lower the pool size if the
backend runs more pods. External Secrets (installed by
`external-secrets.yml`, watching only the app namespace) copies it into the
`hospital-backend-secrets` Secret every hour. It authenticates as the
`backend-secrets-reader` service account, whose IAM role can read only this
secret; no pod runs as it.

Change these values in `.env.local`, not in Secrets Manager. Every bootstrap,
including every rebuild, stores `.env.local`'s values again, so a value changed
only in Secrets Manager is lost. Push the file's values to a running stack:

```bash
(set -a; source .env.local; ansible-playbook ansible/playbooks/backend-secret.yml)
```

The playbook stores both values if they differ from the stored ones, makes
External Secrets sync at once and restarts the backend if the Secret changed
since its pods started (the Deployment records the Secret's hash). A
Deployment scaled to 0 stays at 0 and reads the new Secret when it scales up.
`./scripts/monitoring.sh workloads` warns when the pods run an older Secret.
To change the JWT key, see [Rotate the JWT key](#rotate-the-jwt-key).

To change the database password on a running stack, change it in
`.env.local`, raise `password_wo_version` in `terraform/rds.tf` so Terraform
sends it again, run `./scripts/tf.sh apply`, then push the new connection
string with the command above. Between the apply and the push the backend's
new connections fail. A new stack needs none of this: it takes whatever
`.env.local` holds.

### Playbooks

Apply the Kubernetes manifests:

```bash
ansible-playbook ansible/playbooks/apply-kubernetes.yml
```

This applies the namespace, ConfigMap, Services, HPAs, deploy RBAC,
admission policies and Ingress first (`kubernetes_manifests`). It then waits
for the Load Balancer Controller to create a TargetGroupBinding for each Service
behind the Ingress and for the ALB to become active, and only then applies the
Deployments (`kubernetes_workload_manifests`). The controller adds the ALB
readiness gate only to pods created after their Service's TargetGroupBinding
exists, so this order gives every pod the gate on a fresh cluster. After the
rollouts finish, the playbook restarts any Deployment that still has a pod
without the gate and fails if the new pods don't get it either.

The Ingress explicitly selects `ELBSecurityPolicy-TLS13-1-2-2021-06` and enables
`routing.http.drop_invalid_header_fields.enabled=true`. The ALB accepts TLS
1.2/1.3 and drops invalid HTTP header fields; the annotations are documented
in the [AWS Load Balancer Controller guide](https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/ingress/annotations/).

Install or update the AWS Load Balancer Controller:

```bash
ansible-playbook ansible/playbooks/load-balancer-controller.yml
```

Point the Cloudflare `app.hospitalsyst.cc` CNAME at the current ALB hostname
from the Kubernetes Ingress:

```bash
export CLOUDFLARE_API_TOKEN='your-cloudflare-api-token'
ansible-playbook ansible/playbooks/cloudflare-dns.yml
```

The playbook looks up every record with that name. It updates an existing CNAME,
and replaces a single A or AAAA record with the CNAME, since a CNAME can't share
its name with other records. If there are several records, or one of any other
type, it stops and names them instead of deleting anything.

Run the Kubernetes cleanup that Terraform uses before destroy:

```bash
ansible-playbook ansible/playbooks/cleanup-kubernetes.yml
```

Run the full Kubernetes bootstrap manually:

```bash
ansible-playbook ansible/playbooks/bootstrap.yml
```

Show current Deployment images and Pods:

```bash
ansible-playbook ansible/playbooks/status.yml
```

This is the standalone status playbook. For anything
more (rollout history, HPAs, pod usage, the Ingress, alarms, logs), run the
matching sections of `scripts/monitoring.sh` (see [Monitoring](monitoring.md#monitoring)).

Deploy a specific image tag manually:

```bash
ansible-playbook ansible/playbooks/deploy-image.yml \
  -e image_tag=2026-08-22-8f88481-57-1
```

This sends the same SSM document as CI, so a manual deploy gets the same image
check, smoke test and rollback. It doesn't need the SSM tunnel or `kubectl`.
If the release adds a migration, run `migrate.yml` first (see
[Releases with migrations](cicd.md#releases-with-migrations)).

Load `seed.sql` into an empty database and run the migrations, as the
bootstrap does. Loading the seed needs the password from `.env.local`:

```bash
(set -a; source .env.local; ansible-playbook ansible/playbooks/database.yml)
```

Save the database into `seed.sql`, as the teardown does before it destroys
anything (see [Database](disaster-recovery.md#database)). The old `seed.sql` goes to
`.generated/seed-backups/`:

```bash
(set -a; source .env.local; ansible-playbook ansible/playbooks/dump-database.yml)
```

### Pause and scaling

Stop the app and scale the node group to 0, then bring both back:

```bash
ansible-playbook ansible/playbooks/pause.yml
ansible-playbook ansible/playbooks/resume.yml
```

`pause.yml` turns off the alarm notifications first (with no targets the ALB
answers 503), scales both Deployments to 0 and the node group to 0, and waits
for the nodes to leave. `resume.yml` scales the node group back to two, waits
for both nodes, starts each app at its HPA minimum and turns the notifications
back on once both are ready. The database keeps running and billing while the
app is paused.

Backend and frontend also have HorizontalPodAutoscalers, both fixed at two
pods: one per node, so a drain or node failure leaves one serving. There is no
cluster autoscaler, so the cap matches the two t3.small nodes: memory requests
equal the limits (backend 512Mi, frontend 128Mi), and two pods of each app plus
a rollout surge fit in the nodes' memory. With one node drained, the second
backend pod doesn't fit on the remaining node and waits until the node comes
back, while the PodDisruptionBudget keeps one of each app serving. The HPAs
put any other replica count straight back to two. Scaling to 0 by hand does
stay, but it leaves the alarms on, so stop the app with `pause.yml` instead.

## Database Access

The instance has no public address, and its security group accepts
PostgreSQL (5432) only from the EKS cluster security group, which every node
and pod uses. Nothing else can connect, not your machine and not the ops
instance, so a leaked password is no use on its own. RDS for PostgreSQL 18
also rejects connections without TLS (`rds.force_ssl = 1` in the parameter
group). The same group sets `random_page_cost = 1.1`, the SSD value: with the
default of 4, listing a doctor's appointments read every patient in the table.

To query it, start a `psql` pod in the cluster. Use the `default` namespace:
the app namespace's Pod Security level rejects this image, which runs as root.

```bash
export KUBECONFIG="$PWD/.generated/kubeconfig"
kubectl run pg --image=public.ecr.aws/docker/library/postgres:18.6-alpine --restart=Never -- sleep 3600
kubectl wait --for=condition=Ready pod/pg
kubectl exec -it pg -- psql "host=$(./scripts/tf.sh output -raw db_endpoint) dbname=hospital user=postgres sslmode=require"
kubectl delete pod pg
```

`psql` asks for the password from `.env.local`.

## Operational Notes

Scaling down while not testing only saves the worker nodes (two t3.small,
roughly $30 a month):

- `pause.yml` scales the app and the node group to 0, and `resume.yml`
  brings them back (see [Pause and scaling](#pause-and-scaling)). Terraform
  ignores the node group's desired size, so a later apply won't scale it back
  up.

Everything else keeps billing while the environment exists, roughly $215 a
month before data transfer:

| Resource | Approx. per month |
| -------- | ----------------- |
| EKS control plane | $73 |
| Two NAT gateways | $67 plus data processed |
| SSM interface endpoints | $25 |
| ALB | $17 plus usage |
| Public IPv4 addresses (NAT and ALB) | $15 |
| RDS PostgreSQL (`db.t4g.micro`, 20 GB gp2) | $14 |
| Ops instance (t3.micro) | $9 |

The database's automated backups and its seven days of Performance Insights
cost nothing extra at this size.

To stop paying for it, destroy the environment and recreate it when needed
(see [Disaster Recovery](disaster-recovery.md#disaster-recovery)).

### Rotate the JWT key

```bash
python3 scripts/rotate-jwt-key.py
```

The script generates a random 86-character key and saves it to `.env.local`
before anything else. Only the `HOSPITALSYSTEM_JWT_SECRET` line changes, and
the file ends up readable only by you (mode 0600). The key is never printed,
so nobody types or pastes one. The same command fills in the first key in a
`.env.local` copied from `.env.example`.

- With the stack down (no `ansible/group_vars/all/terraform.yml`), it stops
  there, and the next `./scripts/tf.sh apply` stores the key.
- With the stack up, it runs `backend-secret.yml` with `.env.local`'s values,
  which stores the key in Secrets Manager and restarts the backend one pod at
  a time. It then checks that the Deployment recorded the new Secret and that
  all its pods are ready. This needs `ansible-playbook`, `kubectl`, `aws`,
  `session-manager-plugin` and the database password in `.env.local`.

Every signed-in user is logged out: the backend only accepts the current key,
so older tokens get 401. The script asks first; `--yes` skips the question. If
the playbook fails, the new key is still in `.env.local` and the script prints
the command that pushes it. Run that command rather than the script, which
would generate yet another key.

Rotation is manual, and nothing rotates the secret on a schedule, so Checkov's
CKV2_AWS_57 stays flagged. The script doesn't touch the database password.

### Upgrading EKS

The Kubernetes version is the `eks_version` Terraform variable. EKS upgrades one
minor version at a time, in this order:

1. Raise `eks_version` by one minor version and apply. This upgrades the
   control plane, then moves the vpc-cni, coredns and kube-proxy add-ons
   (`aws_eks_addon.core` in `terraform/modules/eks/main.tf`) to EKS's default
   version for the new Kubernetes version.
2. Run `ansible-playbook ansible/playbooks/apply-kubernetes.yml`, then
   `scripts/monitoring.sh workloads`. The NetworkPolicy lets the control
   plane's network interfaces reach the pods, and `apply-kubernetes.yml` looks
   up their IPs only when it runs. The upgrade doesn't re-run the bootstrap,
   but EKS can replace those interfaces during it. With the old IPs, every
   deploy's smoke test times out through the Service proxy and the deploy
   rolls back. The `workloads` section fails if the policy misses a current
   interface.
3. Update the node group AMI (`aws eks update-nodegroup-version`).
4. Update the Helm charts pinned in `load-balancer-controller.yml` and
   `metrics-server.yml` if their releases require it, and keep the Load
   Balancer Controller IAM policy in `terraform/modules/eks/iam.tf` in step
   with its chart version.

The Kubernetes manifests keep `latest` as the bootstrap/default image tag, but
the CI/CD deployment updates the live Deployments to the tags CI builds.
