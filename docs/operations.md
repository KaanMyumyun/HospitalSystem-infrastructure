# Operations

Running the stack day to day. Everything runs from your machine: Ansible
playbooks wrap `aws`, `kubectl` and `helm`, reach the private EKS API through
an SSM tunnel, and never need SSH access to the nodes.

## Before you start

You need `ansible-playbook`, `aws`, `kubectl`, `helm` and
`session-manager-plugin` (the EKS API endpoint is private; see
[Cluster access](#cluster-access)), and AWS credentials that can access the
`eks-pr1` cluster and start SSM sessions.

Run `./scripts/tf.sh apply` once on a machine before running playbooks by
hand: they read the values Terraform writes, and without them they stop with a
message saying so. Each setting lives in one place:

| File | Holds |
| ---- | ----- |
| `ansible/group_vars/all/terraform.yml` | What Terraform owns, written on every apply and ignored by git: the region, cluster name, namespace, deploy group, VPC ID, ECR repository URLs, IAM role ARNs, ACM certificate ARN, domains, alarm prefix, ops instance ID, and the database endpoint, name and user. |
| `ansible/group_vars/all/main.yml` | What Ansible owns: the Deployment, Ingress and ALB names, the SSM tunnel port, Cloudflare and monitoring options, and the manifest list. The Kubernetes templates and `scripts/monitoring.sh` read these same values. |

## Cluster access

The EKS API has no public endpoint. Playbooks and scripts reach it through an
SSM port-forwarding tunnel via the ops instance, at `https://127.0.0.1:8443`.

To run `kubectl` yourself:

```bash
ansible-playbook ansible/playbooks/kubeconfig.yml   # writes the kubeconfig, opens the tunnel
export KUBECONFIG="$PWD/.generated/kubeconfig"
kubectl get nodes
```

Your `~/.kube/config` and current context are never changed. The tunnel closes
after 12 hours; close it sooner after a rebuild, since it still points at the
old endpoint:

```bash
pkill -f AWS-StartPortForwardingSessionToRemoteHost
```

**How it works**

| Piece | What it does |
| ----- | ------------ |
| `ansible/tasks/kubeconfig.yml` | Opens the tunnel on `eks_tunnel_local_port` and writes the kubeconfig to `kubeconfig_path`. Every playbook that talks to the cluster uses it, and so do `scripts/monitoring.sh`, `scripts/rollout-check.sh` and `scripts/pre-destroy-cleanup.sh`. |
| Ops instance | Accepts no inbound connections and has no internet access: its security group allows HTTPS only inside the VPC and to S3. |
| VPC endpoints | `ssm`, `ssmmessages` and `ec2messages` for Systems Manager (one AZ, roughly $25 a month); the S3 gateway endpoint for the Amazon Linux package repositories. |
| EKS access entry | `aws_eks_access_entry.ops` gives the ops instance's role its Kubernetes group; bootstrap waits for it before applying the deploy RBAC binding. There's no `aws-auth` playbook to run. |

The deploy document runs on the ops instance without `kubectl`: it calls the
Kubernetes API with Python's standard library, using the cluster endpoint and
CA that Terraform writes into the document.

## Backend secret

The backend's connection string and JWT key live in the AWS Secrets Manager
secret `hospitalsystem/backend` (`terraform/secrets.tf`), encrypted with the
project's KMS key. Terraform creates the secret but never its value, so the
value stays out of Terraform state.

**Change these values in `.env.local`, not in Secrets Manager.** Every
bootstrap, including every rebuild, stores `.env.local`'s values again, so a
value changed only in Secrets Manager is lost. To push the file's values to a
running stack:

```bash
(set -a; source .env.local; ansible-playbook ansible/playbooks/backend-secret.yml)
```

The playbook stores both values if they differ from the stored ones, makes
External Secrets sync at once, and restarts the backend if the Secret changed
since its pods started (the Deployment records the Secret's hash). A Deployment
scaled to 0 stays at 0 and reads the new Secret when it scales up.
`./scripts/monitoring.sh workloads` warns when the pods run an older Secret.
To change the JWT key, see [Rotate the JWT key](#rotate-the-jwt-key).

To change the database password on a running stack:

1. Change it in `.env.local`.
2. Raise `password_wo_version` in `terraform/rds.tf`, so Terraform sends it
   again, and run `./scripts/tf.sh apply`.
3. Push the new connection string with the command above. Until you do, the
   backend's new connections fail.

A new stack needs none of this: it takes whatever `.env.local` holds.

**How it works**

- `backend-secret.yml` builds the connection string from the database endpoint
  Terraform reports and `HOSPITALSYSTEM_DB_PASSWORD`, with `SSL Mode=Require`
  (RDS requires TLS) and `Maximum Pool Size=20`.
- External Secrets, installed by `external-secrets.yml` and watching only the
  app namespace, copies the secret into the `hospital-backend-secrets` Secret
  every hour. It authenticates as the `backend-secrets-reader` service account,
  whose IAM role can read only this secret; no pod runs as it.
- **Pool size.** Each backend pod keeps its own pool, so pods × pool size has
  to stay under what the database allows: `max_connections` is 79 on a
  `db.t4g.micro`. With Npgsql's default of 100 per pod, a load test made the
  database swap and fail at 160 requests/s. Lower the pool size if the backend
  runs more pods.

## Playbooks

| Playbook | Use it to |
| -------- | --------- |
| `bootstrap.yml` | Run the whole Kubernetes bootstrap by hand; Terraform runs it on apply |
| `apply-kubernetes.yml` | Apply the Kubernetes manifests |
| `load-balancer-controller.yml` | Install or update the AWS Load Balancer Controller |
| `cloudflare-dns.yml` | Point the `app.hospitalsyst.cc` CNAME at the current ALB |
| `deploy-image.yml` | Deploy an image tag by hand |
| `migrate.yml` | Apply a release's migrations before deploying it (see [Releases with migrations](cicd.md#releases-with-migrations)) |
| `backend-secret.yml` | Push `.env.local`'s values to the backend secret (see [Backend secret](#backend-secret)) |
| `database.yml` | Load `seed.sql` into an empty database and run the migrations |
| `dump-database.yml` | Save the database into `seed.sql` |
| `status.yml` | Show the Deployment images and pods |
| `pause.yml`, `resume.yml` | Stop the app and its nodes, and bring them back (see [Pause and scaling](#pause-and-scaling)) |
| `monitoring.yml` | Create or refresh the ALB alarms (see [Monitoring](monitoring.md#monitoring)) |
| `cleanup-kubernetes.yml` | Run the Kubernetes cleanup Terraform runs before a destroy |
| `kubeconfig.yml` | Only write the kubeconfig and open the tunnel |

Run them from the repository root. The simple ones:

```bash
ansible-playbook ansible/playbooks/bootstrap.yml
ansible-playbook ansible/playbooks/load-balancer-controller.yml
ansible-playbook ansible/playbooks/cleanup-kubernetes.yml
ansible-playbook ansible/playbooks/status.yml
```

`status.yml` shows only the Deployment images and pods. For rollout history,
HPAs, pod usage, the Ingress, alarms or logs, run the matching sections of
`scripts/monitoring.sh` (see [Monitoring](monitoring.md#monitoring)). The rest
need a word of explanation.

### Applying the manifests

```bash
ansible-playbook ansible/playbooks/apply-kubernetes.yml
```

The order matters on a fresh cluster:

1. The namespace, ConfigMap, Services, HPAs, deploy RBAC, admission policies
   and Ingress (`kubernetes_manifests`).
2. A wait for the Load Balancer Controller to create a TargetGroupBinding for
   each Service behind the Ingress, and for the ALB to become active.
3. The Deployments (`kubernetes_workload_manifests`).

The controller adds the ALB readiness gate only to pods created after their
Service's TargetGroupBinding exists, so this order gives every pod the gate.
After the rollouts finish, the playbook restarts any Deployment that still has
a pod without the gate, and fails if the new pods don't get it either.

The Ingress explicitly selects `ELBSecurityPolicy-TLS13-1-2-2021-06` and enables
`routing.http.drop_invalid_header_fields.enabled=true`, so the ALB accepts TLS
1.2/1.3 and drops invalid HTTP header fields; the annotations are documented in
the [AWS Load Balancer Controller guide](https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/ingress/annotations/).

### Pointing DNS at the ALB

```bash
export CLOUDFLARE_API_TOKEN='your-cloudflare-api-token'
ansible-playbook ansible/playbooks/cloudflare-dns.yml
```

The playbook takes the ALB hostname from the Kubernetes Ingress and looks up
every record named `app.hospitalsyst.cc`. It updates an existing CNAME, and
replaces a single A or AAAA record with the CNAME, since a CNAME can't share
its name with other records. If there are several records, or one of any other
type, it stops and names them instead of deleting anything.

### Deploying by hand

```bash
ansible-playbook ansible/playbooks/deploy-image.yml \
  -e image_tag=2026-08-22-8f88481-57-1
```

This sends the same SSM document as CI, so a manual deploy gets the same image
check, smoke test and rollback. It doesn't need the SSM tunnel or `kubectl`. If
the release adds a migration, run `migrate.yml` first.

### Loading and saving the database

Both need the database password from `.env.local`:

```bash
(set -a; source .env.local; ansible-playbook ansible/playbooks/database.yml)       # load seed.sql, as the bootstrap does
(set -a; source .env.local; ansible-playbook ansible/playbooks/dump-database.yml)  # save into seed.sql, as the teardown does
```

The dump keeps the old `seed.sql` in `.generated/seed-backups/`. How the data
carries over between stacks is in [Database](disaster-recovery.md#database).

## Pause and scaling

Stop the app and scale the node group to 0, then bring both back:

```bash
ansible-playbook ansible/playbooks/pause.yml
ansible-playbook ansible/playbooks/resume.yml
```

- `pause.yml` turns off the alarm notifications first (with no targets the ALB
  answers 503), scales both Deployments and the node group to 0, and waits for
  the nodes to leave.
- `resume.yml` scales the node group back to two, waits for both nodes, starts
  each app at its HPA minimum, and turns the notifications back on once both
  are ready.
- The database keeps running, and billing, while the app is paused. Terraform
  ignores the node group's desired size, so a later apply won't scale it back
  up.

Both apps also have HorizontalPodAutoscalers fixed at two pods, one per node,
so a drain or node failure leaves one serving:

- There's no cluster autoscaler, so the cap matches the two t3.small nodes:
  memory requests equal the limits (backend 512Mi, frontend 128Mi), and two
  pods of each app plus a rollout surge fit in the nodes' memory.
- With one node drained, the second backend pod doesn't fit on the remaining
  node and waits until the node comes back, while the PodDisruptionBudget keeps
  one of each app serving.
- The HPAs put any other replica count straight back to two. A manual scale to
  0 does stay, but it leaves the alarms on, so stop the app with `pause.yml`
  instead.

## Database Access

To query the database, start a `psql` pod in the cluster. Use the `default`
namespace: the app namespace's Pod Security level rejects this image, which
runs as root.

```bash
export KUBECONFIG="$PWD/.generated/kubeconfig"
kubectl run pg --image=public.ecr.aws/docker/library/postgres:18.6-alpine --restart=Never -- sleep 3600
kubectl wait --for=condition=Ready pod/pg
kubectl exec -it pg -- psql "host=$(./scripts/tf.sh output -raw db_endpoint) dbname=hospital user=postgres sslmode=require"
kubectl delete pod pg
```

`psql` asks for the password from `.env.local`.

Nothing outside the cluster can connect, not your machine and not the ops
instance, so a leaked password is no use on its own:

- The instance has no public address, and its security group accepts
  PostgreSQL (5432) only from the EKS cluster security group, which every node
  and pod uses.
- RDS for PostgreSQL 18 rejects connections without TLS
  (`rds.force_ssl = 1` in the parameter group).

The same parameter group sets `random_page_cost = 1.1`, the SSD value: with the
default of 4, listing a doctor's appointments read every patient in the table.

## Costs

The stack bills roughly $215 a month before data transfer whether the app runs
or not:

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

Pausing saves only the worker nodes, two t3.small at roughly $30 a month (see
[Pause and scaling](#pause-and-scaling)). To stop paying for the rest, destroy
the environment and recreate it when needed (see
[Disaster Recovery](disaster-recovery.md#disaster-recovery)).

## Rotate the JWT key

```bash
python3 scripts/rotate-jwt-key.py
```

**Every signed-in user is logged out:** the backend only accepts the current
key, so older tokens get 401. The script asks first; `--yes` skips the
question.

What it does:

1. Generates a random 86-character key and saves it to `.env.local` before
   anything else. Only the `HOSPITALSYSTEM_JWT_SECRET` line changes, the file
   ends up readable only by you (mode 0600), and the key is never printed, so
   nobody types or pastes one. The same command fills in the first key in a
   `.env.local` copied from `.env.example`.
2. With the stack down (no `ansible/group_vars/all/terraform.yml`), stops
   there; the next `./scripts/tf.sh apply` stores the key.
3. With the stack up, runs `backend-secret.yml` with `.env.local`'s values,
   which stores the key in Secrets Manager and restarts the backend one pod at
   a time, then checks that the Deployment recorded the new Secret and that all
   its pods are ready. This needs `ansible-playbook`, `kubectl`, `aws`,
   `session-manager-plugin` and the database password in `.env.local`.

If the playbook fails, the new key is still in `.env.local` and the script
prints the command that pushes it. Run that command rather than the script,
which would generate yet another key.

Rotation is manual, and nothing rotates the secret on a schedule, so Checkov's
CKV2_AWS_57 stays flagged. The script doesn't touch the database password.

## Upgrading EKS

The Kubernetes version is the `eks_version` Terraform variable. EKS upgrades one
minor version at a time, in this order:

1. Raise `eks_version` by one minor version and apply. This upgrades the
   control plane, then moves the vpc-cni, coredns and kube-proxy add-ons
   (`aws_eks_addon.core` in `terraform/modules/eks/main.tf`) to EKS's default
   version for the new Kubernetes version.
2. Run `ansible-playbook ansible/playbooks/apply-kubernetes.yml`, then
   `scripts/monitoring.sh workloads`. The NetworkPolicy lets the control plane's
   network interfaces reach the pods, and `apply-kubernetes.yml` looks up their
   IPs only when it runs. EKS can replace those interfaces during the upgrade,
   which doesn't re-run the bootstrap; with the old IPs, every deploy's smoke
   test times out through the Service proxy and the deploy rolls back. The
   `workloads` section fails if the policy misses a current interface.
3. Update the node group AMI (`aws eks update-nodegroup-version`).
4. Update the Helm charts pinned in `load-balancer-controller.yml` and
   `metrics-server.yml` if their releases require it, and keep the Load
   Balancer Controller IAM policy in `terraform/modules/eks/iam.tf` in step
   with its chart version.
