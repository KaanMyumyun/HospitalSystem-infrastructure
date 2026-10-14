# Terraform Workflow

Terraform is the source of truth for the AWS layer: it builds the whole
environment from an empty account, so there's nothing to import first. Run it
through `./scripts/tf.sh`, which loads your settings and manages the state
bucket. Check and review a change before applying it:

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
to fit under (see [Backend secret](operations.md#backend-secret)). The private
subnets' CIDRs follow the number of zones, so changing `availability_zones`
replaces them and everything in them; change it with the stack torn down.

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

- **Database password.** Terraform sets it on a new instance, and the bootstrap
  builds the backend's connection string from it and the endpoint Terraform
  reports, so there's no connection string to keep in sync. RDS accepts 8 to
  128 characters without `/`, `'`, `"`, `@` or spaces; this makes one:

  ```bash
  python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
  ```

- **JWT key.** Don't make it up: `python3 scripts/rotate-jwt-key.py` generates
  it into `.env.local` (see [Rotate the JWT key](operations.md#rotate-the-jwt-key)).
- **Cloudflare token.** It needs `Zone:Read` and `DNS:Edit` permissions for
  `hospitalsyst.cc`: Terraform uses it for the ACM certificate validation
  record, and the bootstrap for the app's CNAME.

`./scripts/tf.sh` loads `.env.local`, exports `TF_VAR_cloudflare_api_token`,
`TF_VAR_alert_email` and `TF_VAR_db_password`, and checks only what the command
needs:

| Command | Requires |
| ------- | -------- |
| `apply` | all four required values above, a running Docker daemon and `session-manager-plugin` |
| `destroy` | the Cloudflare token and `session-manager-plugin` |
| `plan`, `refresh`, `import` | the Cloudflare token and the database password |
| `output`, `state` and other read-only commands | nothing |

Every command except `fmt` and `validate` also needs an AWS login, since the
state is in S3. The database password is a write-only argument (`password_wo`
in `terraform/rds.tf`): Terraform sends it to RDS but never writes it to the
state or a plan, so a destroy doesn't need it.

Skipping `tf.sh` is how most setup problems start: the Ansible bootstrap
sources `.env.local` itself, but the Cloudflare provider reads its token from
the environment, so a plain `terraform apply` without
`TF_VAR_cloudflare_api_token` exported fails with
`403 Missing X-Auth-Email header` before creating anything.

## Remote state

The state lives in the S3 bucket `hospitalsystem-tfstate-<account ID>`, under
`hospitalsystem/terraform.tfstate`. The bucket exists only as long as the stack,
so nothing is left billing after a teardown:

- **Created** by `./scripts/tf.sh apply` (and `plan`, `init`, `import`) from
  `terraform/bootstrap` when it's missing, which then runs `terraform init`
  against it when this checkout isn't set up for it yet.
- **Deleted** by `./scripts/teardown.sh` after the destroy, with every state
  version in it, but only when the state is empty and unlocked. Otherwise the
  bucket stays and the teardown reports it.
- **Locked** with a `.tflock` object next to the state (`use_lockfile`), so no
  DynamoDB table is needed.
- **Protected:** versioned while the stack is up, so an earlier state can be
  restored; encrypted with SSE-S3; private, and reachable only over TLS.

Without the bucket, `./scripts/tf.sh output`, `state` and `destroy` stop and say
there's no state.

`terraform/bootstrap` is a separate configuration because the bucket can't
hold the state of the configuration that creates it. Its own state is local
(`terraform/bootstrap/terraform.tfstate`, ignored by git). If that file is lost
while the bucket exists, the teardown can't delete the bucket and says so;
empty and delete it in the S3 console.

**A stale lock.** If an interrupted run leaves the lock behind, the next run
fails with `Error acquiring the state lock` and prints the lock ID. When
nothing is still running, remove it with `./scripts/tf.sh force-unlock <ID>`.

## What an apply does

On a fresh AWS account, `./scripts/tf.sh apply`:

1. Detects the active account ID and creates the AWS resources.
2. Builds and pushes the first backend and frontend images from the local
   application checkout. It skips any image whose `latest` tag is already in
   ECR, so a later apply doesn't replace what CI pushed.
3. Requests the ACM certificate and validates it through Cloudflare DNS.
4. Runs the Ansible bootstrap (`ansible/playbooks/bootstrap.yml`) once the EKS
   cluster, managed node group, ops access entry and database are ready. The
   bootstrap:
   - configures kubeconfig and installs External Secrets
   - stores the backend secret from `.env.local` in Secrets Manager (see
     [Backend secret](operations.md#backend-secret))
   - loads `seed.sql` into a new database and runs the migrations (see
     [Database](disaster-recovery.md#database))
   - installs the AWS Load Balancer Controller and metrics-server
   - applies the Kubernetes manifests
   - points the Cloudflare DNS record at the ALB hostname from the Kubernetes
     Ingress
   - configures monitoring

The bootstrap is intentionally local: it uses the `aws`, `docker`, `kubectl`,
`helm` and `ansible-playbook` binaries on the machine running Terraform.

### The first images

Each bootstrap image is pushed under two tags: `latest`, which the Kubernetes
manifests start from, and a `<date>-<short sha>` tag, the deploy workflow's
format without its run number and attempt. So the image the cluster first runs
is traceable to the commit it was built from. The short SHA comes from the
application checkout and gains a `-dirty` suffix when that working tree has
uncommitted or untracked files. The image also carries
`org.opencontainers.image.revision` and `org.opencontainers.image.created`
labels.

The deploy workflow moves the live Deployments from `latest` to the tag CI
built after the first CI/CD run.

### Later applies

The bootstrap runs again when its generated variables or tracked inputs change:
its playbooks, shared tasks and variables, Ansible configuration and inventory,
the Kubernetes templates, and the workload apply helper. Editing the
independent deploy, scale, cleanup or status playbooks doesn't trigger it.

A later bootstrap keeps each Deployment's live release image and replica count,
including a rollback or an intentional scale to zero. Only new Deployments
receive the initial image, and replicas are left to Kubernetes and the HPA. To
change images use `deploy-image.yml` or CI, and to stop and start the app and
its nodes use `pause.yml`/`resume.yml`. Failed or malformed Kubernetes reads
stop the apply.

How it keeps them: `scripts/apply-workload.py` removes image and replica
ownership from the old last-applied annotation before omitting those fields on
updates, following the
[Kubernetes field ownership migration procedure](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/declarative-config/#changing-the-owner-from-a-configuration-file-to-a-direct-imperative-writer).
That avoids resetting replicas during the first upgrade and overwriting a CI
image change during the bootstrap.

Two variables skip parts of an apply:

```bash
./scripts/tf.sh apply -var run_ansible_bootstrap=false    # AWS resources only
./scripts/tf.sh apply -var push_initial_ecr_images=false  # no local image build and push
```

- Skipping the bootstrap is safe on a running stack: what the bootstrap
  created, including the Ingress and its ALB, stays up. Setting it back to
  `true` runs the bootstrap again. The Kubernetes cleanup still runs on destroy
  either way.
- `./scripts/tf.sh` still checks for the Docker daemon on every `apply`, even
  with `push_initial_ecr_images=false`.

### On destroy

Before deleting EKS, Terraform runs an Ansible cleanup that deletes the
Kubernetes Ingress and waits for the AWS-managed ALB to be removed. If the AWS
Load Balancer Controller is unavailable, the cleanup deletes the ALB and the
Kubernetes-managed security groups directly, so unmanaged ALB network
interfaces and security groups don't block deleting the VPC. The rest of the
teardown is in [Disaster Recovery](disaster-recovery.md#disaster-recovery).
