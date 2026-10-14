# Terraform Workflow

Terraform is the source of truth for the AWS layer. It creates the whole
environment from an empty account (see [Disaster Recovery](disaster-recovery.md#disaster-recovery)),
so there is nothing to import first. Check and review a change before applying
it:

```bash
./scripts/tf.sh fmt -check -recursive
./scripts/tf.sh plan    # validates too; the first run creates the state bucket
./scripts/tf.sh apply
```

## Layout and sizing

The root module in `terraform/` calls two local modules and holds the rest of
the stack:

| Where | What |
| ----- | ---- |
| `terraform/modules/network` | VPC, a public and a private subnet per availability zone, NAT gateways, route tables, VPC endpoints and flow logs |
| `terraform/modules/eks` | Cluster, node group, add-ons, the cluster's OIDC provider, and the cluster, node and Load Balancer Controller IAM roles |
| `terraform/*.tf` | The database, KMS key, ECR, ACM, Secrets Manager, the ops instance, the GitHub Actions roles, monitoring and the Ansible bootstrap |

The sizes are variables in `terraform/variables.tf`. The defaults are the sizes
the stack has always run with:

| Variable | Default |
| -------- | ------- |
| `availability_zones` | `["a", "b"]` |
| `single_nat_gateway` | `false`: a NAT gateway per zone |
| `node_instance_types` | `["t3.small"]` |
| `node_desired_size`, `node_max_size` | `2`, `2` |
| `db_instance_class` | `db.t4g.micro` |
| `db_multi_az` | `false` |
| `db_backup_retention_days` | `1` |

The database class sets `max_connections`, which the backend pods' pools have
to fit under (see [Backend secret](operations.md#backend-secret)). The private subnets'
CIDRs follow the number of zones, so changing `availability_zones` replaces
them and everything in them; change it with the stack torn down.

## Local setup

Put these values in the ignored `.env.local` (start from `.env.example`), or
export them before running Terraform:

```bash
HOSPITALSYSTEM_DB_PASSWORD=...         # the database's master password
HOSPITALSYSTEM_JWT_SECRET=...          # generated, see below
CLOUDFLARE_API_TOKEN='your-cloudflare-api-token'
ALERT_EMAIL=you@example.com            # receives the alarm emails
CLOUDFLARE_ZONE_ID='your-zone-id'      # optional, skips the zone lookup
```

Terraform sets the database password on a new instance, and the bootstrap
builds the backend's connection string from it and the endpoint Terraform
reports, so there's no connection string to keep in sync. RDS accepts 8 to 128
characters without `/`, `'`, `"`, `@` or spaces; this makes one:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
```

Don't make up the JWT key: `python3 scripts/rotate-jwt-key.py` generates it
into `.env.local` (see [Rotate the JWT key](operations.md#rotate-the-jwt-key)).

The Cloudflare token needs `Zone:Read` and `DNS:Edit` permissions for
`hospitalsyst.cc`. Terraform uses it for the ACM certificate validation
record, and the bootstrap for the app's CNAME.

Run Terraform through `./scripts/tf.sh`. It loads `.env.local`, exports
`TF_VAR_cloudflare_api_token`, `TF_VAR_alert_email` and `TF_VAR_db_password`,
and checks only what the command needs:

| Command | Requires |
| ------- | -------- |
| `apply` | all four required values above, a running Docker daemon and `session-manager-plugin` |
| `destroy` | the Cloudflare token and `session-manager-plugin` |
| `plan`, `refresh`, `import` | the Cloudflare token and the database password |
| `output`, `state` and other read-only commands | nothing |

The password is a write-only argument (`password_wo` in `terraform/rds.tf`):
Terraform sends it to RDS but never writes it to the state or a plan. A destroy
doesn't need it.

Every command except `fmt` and `validate` also needs an AWS login, since the
state is in S3. The Ansible bootstrap sources `.env.local` itself, but the
Cloudflare provider reads its token from the environment: a plain
`terraform apply` without `TF_VAR_cloudflare_api_token` exported fails with
`403 Missing X-Auth-Email header` before creating anything.

## Remote state

The state lives in the S3 bucket `hospitalsystem-tfstate-<account ID>`, under
`hospitalsystem/terraform.tfstate`, and the bucket lives only as long as the
stack, so nothing is left billing after a teardown:

- `./scripts/tf.sh apply` (and `plan`, `init`, `import`) creates the bucket
  from `terraform/bootstrap` when it is missing, then runs `terraform init`
  against it when this checkout isn't set up for it yet.
- `./scripts/teardown.sh` deletes the bucket, with every state version in it,
  after the destroy, and only when the state is empty and unlocked. If the
  state still holds anything, the bucket stays and the teardown reports it.

So a full cycle is still two commands, with nothing to set up by hand:

```bash
./scripts/tf.sh apply
./scripts/teardown.sh
```

Terraform locks the state with a `.tflock` object next to it
(`use_lockfile`), so no DynamoDB table is needed. While the stack is up the
bucket is versioned, so an earlier state can be restored. It is encrypted
with SSE-S3, private, and reachable only over TLS.

`terraform/bootstrap` is a separate configuration because the bucket can't
hold the state of the configuration that creates it. Its own state is local
(`terraform/bootstrap/terraform.tfstate`, ignored by git). If that file is
lost while the bucket exists, the teardown can't delete the bucket and says
so; empty and delete it in the S3 console.

Without the bucket, `./scripts/tf.sh output`, `state` and `destroy` stop and
say there is no state. `fmt` and `validate` don't touch AWS.

If an interrupted run leaves the lock behind, the next run fails with
`Error acquiring the state lock` and prints the lock ID. When nothing is still
running, remove it with `./scripts/tf.sh force-unlock <ID>`.

## What an apply does

On a fresh AWS account, Terraform detects the active account ID, creates the
ECR repositories, builds and pushes initial backend/frontend `latest` images
from the local application source (skipping any image whose `latest` tag is
already in ECR, so a later apply doesn't replace what CI pushed), requests
and validates the ACM certificate through Cloudflare DNS, and then runs the
Ansible Kubernetes bootstrap.

Each bootstrap image is pushed under two tags: `latest`, which the Kubernetes
manifests start from, and a `<date>-<short sha>` tag, the deploy workflow's
format without its run number and attempt, so the image the cluster first runs
is traceable to the commit it was built from. The short SHA comes from the
application checkout and gains a `-dirty` suffix when that working tree has
uncommitted or untracked files. The image also carries
`org.opencontainers.image.revision` and `org.opencontainers.image.created`
labels.

Terraform runs the Ansible bootstrap (`ansible/playbooks/bootstrap.yml`)
automatically after the EKS cluster, managed node group, and ops access entry
are ready. The bootstrap configures kubeconfig, installs External Secrets,
stores the backend secret from `.env.local` in Secrets Manager (see
[Backend secret](operations.md#backend-secret)), loads `seed.sql` into a new database and
runs the migrations (see [Database](disaster-recovery.md#database)), installs the AWS Load
Balancer Controller and metrics-server, applies the Kubernetes manifests,
points the Cloudflare DNS record at the ALB hostname from the Kubernetes
Ingress, and configures monitoring. Terraform starts it only once the
database is available.

Bootstrap reruns when its generated variables or tracked inputs change:
its playbooks, shared tasks and variables, Ansible configuration/inventory,
Kubernetes templates, and the workload apply helper. Editing independent
deploy, scale, cleanup, or status playbooks does not trigger bootstrap.

After a fresh Terraform bootstrap, the Kubernetes manifests start the app from
the bootstrap/default `latest` image tag. The GitHub Actions deploy workflow
updates the live Deployments to the tag CI built after CI/CD runs.

Later bootstraps preserve each Deployment's live release image and replica
count, including a rollback or an intentional scale to zero. Only new
Deployments receive the initial image; replicas are left to Kubernetes and
the HPA. `scripts/apply-workload.py` removes image and replica ownership from
the old last-applied annotation before omitting those fields on updates,
following the [Kubernetes field ownership migration procedure](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/declarative-config/#changing-the-owner-from-a-configuration-file-to-a-direct-imperative-writer).
This avoids resetting replicas during the first upgrade and avoids overwriting
a CI image change during bootstrap. Failed or malformed Kubernetes reads stop
the apply. Use `deploy-image.yml` or CI to change images, and
`pause.yml`/`resume.yml` to stop and start the app and its nodes.

If you only want Terraform to manage AWS resources and skip the Ansible
bootstrap:

```bash
./scripts/tf.sh apply -var run_ansible_bootstrap=false
```

This is safe on a running stack: what the bootstrap created, including the
Ingress and its ALB, stays up. Setting it back to `true` runs the bootstrap
again. The Kubernetes cleanup still runs on destroy either way.

If you want to skip the local Docker image build/push during a later apply:

```bash
./scripts/tf.sh apply -var push_initial_ecr_images=false
```

`./scripts/tf.sh` still checks for the Docker daemon on every `apply`, even
with this variable set.

The automatic bootstrap is intentionally local. It uses your local `aws`,
`docker`, `kubectl`, `helm`, and `ansible-playbook` binaries from the machine
where Terraform is running.

Terraform also runs a destroy-time Ansible cleanup before deleting EKS. That
cleanup deletes the Kubernetes Ingress and waits for the AWS-managed ALB to be
removed. If the AWS Load Balancer Controller is unavailable, the cleanup falls
back to deleting the ALB and Kubernetes-managed security groups directly, so
unmanaged ALB ENIs and security groups do not block VPC deletion.
