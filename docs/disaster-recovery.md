# Disaster Recovery

The full recreate path, a destroy followed by an apply, has been tested.
Terraform recreates the AWS layer, pushes the initial ECR images from local
source, validates ACM through Cloudflare, then runs the local Ansible bootstrap
to configure Kubernetes and Cloudflare. The apply needs the values from
[Local setup](terraform.md#local-setup) in `.env.local` or the environment.

Full recreate:

```bash
./scripts/teardown.sh
./scripts/tf.sh apply
```

`./scripts/teardown.sh` runs every step of a clean destroy. It starts right
away, without asking:

1. checks the AWS login, finds the logged-in account's state bucket, and
   checks that no Terraform run holds the state lock (if one does, it shows
   who took the lock, when, and the `force-unlock` command for an interrupted
   run). With no state bucket there is nothing in state, so it skips to step 6
2. saves the database into `seed.sql` (`ansible/playbooks/dump-database.yml`),
   so the next stack starts with this one's data (see [Database](#database)).
   `--skip-dump` destroys the database without saving it
3. destroys `terraform_data.kubernetes_cleanup` alone, so the cleanup below
   runs while the cluster can still delete its ALB
4. runs `scripts/pre-destroy-cleanup.sh --apply` for anything left, retried
   once
5. runs `./scripts/tf.sh destroy`; if it fails, it runs step 4 again,
   deletes the EKS cluster security group EKS sometimes leaves in the VPC,
   and retries once
6. checks the state is empty and runs `scripts/cleanup-orphans.sh --apply`
7. deletes the state bucket, only if the state is empty and unlocked
8. runs `scripts/account-sweep.py` over every region, which lists a state
   bucket still there as `BILLABLE`

It stops at the first step that fails, so nothing is destroyed until the
database is saved, and exits 1 unless the stack is gone and nothing billable
is left. Each step's output is also saved under `.generated/teardown/`.

`./scripts/account-sweep.py` also runs on its own. It is read-only and lists
what exists in every enabled region: `BILLABLE` for what costs money (for
example instances, RDS databases and manual snapshots, NAT gateways, Elastic
IPs, interface endpoints, load balancers, EKS, enabled KMS keys, secrets, log
groups holding data), `FREE`
for what doesn't (keys pending deletion, target groups, idle Lambda
functions), and `IGNORED` for entries in `config/account-sweep-ignore.txt`,
which says why each is kept. `--region` limits it to one region, and
`--cost` adds this month's spend without credits (one Cost Explorer call,
$0.01; its data lags about a day).

During destroy, Terraform runs `ansible/playbooks/cleanup-kubernetes.yml`
before it deletes the cluster. The playbook removes the app's Cloudflare CNAME
if it still points at this ALB, deletes the Ingress so the Load Balancer
Controller deletes the ALB (or deletes the ALB directly when the cluster can't
be reached), then removes the ALB's target groups and `k8s-*` security groups
and the ALB alarms. The cluster, the ops instance, the controller's IAM role
and OIDC provider, and the NAT gateways and routes stay until it has finished.
A resource that is already gone counts as done; any other error stops the
destroy, before it could stall on an ALB that is still up. Fix the cause and
run the destroy again.

`scripts/pre-destroy-cleanup.sh --apply` does the same AWS cleanup by hand. It
also stops on lookup errors, retries target groups and security groups that
AWS still reports in use, and exits non-zero if anything is left.

Destroy can still leave what Terraform never tracked: the controller's target
groups, the monitoring playbook's ALB alarms if neither cleanup reached them,
and KMS keys waiting out their deletion window. `./scripts/cleanup-orphans.sh`
lists them and `--apply` deletes them. It only picks target groups that carry
the controller's tags for this cluster and Ingress and whose VPC no longer
exists, and only the ALB alarms `config/alb-alarms.json` defines (the same
file `monitoring.yml` creates them from), so another project's resources and
Terraform's own alarm are left alone. Any failed AWS lookup stops it before it
deletes anything else.

During apply, Terraform recreates AWS resources and then runs
`ansible/playbooks/bootstrap.yml`. The complete apply path:

- detects the active AWS account ID
- creates ECR repositories for backend and frontend images
- builds and pushes backend/frontend `latest` images from local source, unless
  ECR already has them
- creates the GitHub Actions OIDC provider and IAM roles
- creates the EKS cluster and node group, and takes over the vpc-cni, coredns
  and kube-proxy add-ons as EKS managed add-ons
- creates the ops role's EKS access entry
- creates the RDS instance in the private subnets, alongside the cluster
- requests the ACM certificate for `app.hospitalsyst.cc`
- creates the Cloudflare ACM validation CNAME
- waits for ACM to issue the certificate
- installs External Secrets, stores the backend secret in Secrets Manager
  from local environment variables and waits for it to sync
- loads `seed.sql` into the new, empty database and runs the migrations
- installs the AWS Load Balancer Controller
- applies rendered app Kubernetes manifests with current ECR URLs and ACM ARN
- updates the Cloudflare app CNAME to the new ALB hostname
- waits for running Deployment rollouts and configures monitoring

After a fresh recreate, Kubernetes starts from the bootstrap/default `latest`
image tag, and the new ECR repositories hold only those `latest` images. No
earlier release tag is in them, so re-running an old `Deploy to EKS` run fails
its ECR check. Build a release into the new repositories first: re-run the
`Docker Image CI` run for the commit at the tip of `main` (the new repositories
have none of its tags, so its push goes through; the deploy skips older
commits), or push a commit to `main`. The `Deploy to EKS` run that follows
moves the live Deployments to the new tag after the `production` approval.

Post-recreate checks:

```bash
ansible-playbook ansible/playbooks/status.yml
export KUBECONFIG="$PWD/.generated/kubeconfig"
kubectl get ingress hospital-ingress -n hospitalsystem -o wide
dig +short app.hospitalsyst.cc CNAME
```

## Database

The database lives as long as the stack. A destroy deletes the RDS instance
with its automated backups and takes no final snapshot, so nothing keeps
billing, and every new stack starts with an empty database. The bootstrap
fills it (`ansible/playbooks/database.yml`) after it has stored the connection
string and before the backend starts:

1. If the database has no tables and `seed.sql` is in the repository root, a
   short-lived `psql` pod loads it in one transaction. The password reaches
   the pod on stdin, so it's neither in the pod spec nor in the API server's
   audit log. A database that has tables is left alone, so a later bootstrap
   never loads it twice.
2. The migration Job applies the migrations of the image the backend runs, or
   is about to start from: on a seeded database the ones newer than the dump,
   and without `seed.sql` the whole schema with no rows. The application
   creates no first user, so without `seed.sql` nobody can sign in.

`seed.sql` is a plain `pg_dump`. Git ignores it because it holds user accounts
and patient records. The teardown writes it from the running database before
it destroys anything (`ansible/playbooks/dump-database.yml`, also runnable on
its own, see [Playbooks](operations.md#playbooks)), so the data carries over to the next
stack:

- The database only accepts connections from the cluster (see
  [Database Access](operations.md#database-access)), so `pg_dump` runs in a short-lived pod
  with the same settings as the seed pod and gets the password on stdin.
- The dump replaces `seed.sql` only if `pg_dump` succeeded and the file ends
  like a complete dump. The old `seed.sql` is kept in the ignored
  `.generated/seed-backups/`, one timestamped file per save.
- The dump comes over gzip'd, and a cut download is tried up to three times.
  The SSM tunnel stops long downloads from the cluster after 4 to 7 MB, and the
  2026-10-14 demo data dumps to 6 MB of SQL but only 1.2 MB gzip'd. Uploads,
  such as loading `seed.sql`, aren't affected. A database whose gzip'd dump
  reaches a few MB, roughly 20 MB of SQL, needs the dump fetched in pieces.
- A database without tables, from a bootstrap that failed before the seed,
  leaves `seed.sql` alone.
- If the dump fails, the teardown stops before it destroys anything. The dump
  needs the nodes: on a paused stack run `resume.yml` first. To destroy the
  database without saving it, run `./scripts/teardown.sh --skip-dump`.

RDS keeps a day of automated backups while the stack is up, but a
point-in-time restore creates a new instance outside Terraform. For this
stack, start over from `seed.sql` instead: rebuild the stack, or scale the
backend to 0, drop and recreate the `hospital` database, and run
`database.yml` (see [Playbooks](operations.md#playbooks)).

The application's always-on demo on Render still uses its own Neon database,
so it shares no data with this stack.
