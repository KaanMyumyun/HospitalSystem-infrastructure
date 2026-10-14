# Disaster Recovery

The whole stack can be destroyed and rebuilt with two commands, and the full
recreate path has been tested. The database's data survives the trip: the
teardown saves it into `seed.sql` before destroying anything, and the next
apply loads it back.

```bash
./scripts/teardown.sh
./scripts/tf.sh apply
```

The apply needs the values from [Local setup](terraform.md#local-setup) in
`.env.local` or the environment. It recreates the AWS layer, pushes the initial
ECR images from local source, validates ACM through Cloudflare and runs the
local Ansible bootstrap; [What an apply does](terraform.md#what-an-apply-does)
has every step.

## What the teardown does

`./scripts/teardown.sh` runs every step of a clean destroy. It starts right
away, without asking:

1. Checks the AWS login, finds the logged-in account's state bucket, and checks
   that no Terraform run holds the state lock. If one does, it shows who took
   the lock, when, and the `force-unlock` command for an interrupted run. With
   no state bucket there is nothing in state, so it skips to step 6.
2. Saves the database into `seed.sql` (`ansible/playbooks/dump-database.yml`),
   so the next stack starts with this one's data (see [Database](#database)).
   `--skip-dump` destroys the database without saving it.
3. Destroys `terraform_data.kubernetes_cleanup` alone, so the cleanup below runs
   while the cluster can still delete its ALB.
4. Runs `scripts/pre-destroy-cleanup.sh --apply` for anything left, retried
   once.
5. Runs `./scripts/tf.sh destroy`. If it fails, it runs step 4 again, deletes
   the EKS cluster security group EKS sometimes leaves in the VPC, and retries
   once.
6. Checks the state is empty and runs `scripts/cleanup-orphans.sh --apply`.
7. Deletes the state bucket, only if the state is empty and unlocked.
8. Runs `scripts/account-sweep.py` over every region, which lists a state
   bucket still there as `BILLABLE`.

It stops at the first step that fails, so nothing is destroyed until the
database is saved, and exits 1 unless the stack is gone and nothing billable is
left. Each step's output is also saved under `.generated/teardown/`.

### What cleans up what

The ALB and its pieces are created by the Load Balancer Controller, not
Terraform, so a plain destroy would stall on them. Four tools make sure nothing
is left:

| Tool | Runs | What it removes |
| ---- | ---- | --------------- |
| `ansible/playbooks/cleanup-kubernetes.yml` | During destroy, before the cluster is deleted | The app's Cloudflare CNAME if it still points at this ALB, then the Ingress, so the controller deletes the ALB (or the ALB directly when the cluster can't be reached), then the ALB's target groups, its `k8s-*` security groups and the ALB alarms |
| `scripts/pre-destroy-cleanup.sh --apply` | Teardown step 4, or by hand | The same AWS cleanup |
| `./scripts/cleanup-orphans.sh` | Teardown step 6, or by hand | Lists what Terraform never tracked, and `--apply` deletes it: the controller's target groups, the ALB alarms if neither cleanup reached them, and KMS keys waiting out their deletion window |
| `./scripts/account-sweep.py` | Teardown step 8, or by hand | Nothing: it's read-only and lists what exists in every enabled region |

How they stay safe:

- **`cleanup-kubernetes.yml`:** the cluster, the ops instance, the controller's
  IAM role and OIDC provider, and the NAT gateways and routes stay until it has
  finished. A resource that's already gone counts as done; any other error
  stops the destroy before it could stall on an ALB that's still up. Fix the
  cause and run the destroy again.
- **`pre-destroy-cleanup.sh`** stops on lookup errors, retries target groups and
  security groups that AWS still reports in use, and exits non-zero if
  anything is left.
- **`cleanup-orphans.sh`** only picks target groups that carry the controller's
  tags for this cluster and Ingress and whose VPC no longer exists, and only
  the ALB alarms `config/alb-alarms.json` defines (the same file
  `monitoring.yml` creates them from). Another project's resources and
  Terraform's own alarm are left alone, and any failed AWS lookup stops it
  before it deletes anything else.

The account sweep sorts what it finds:

- `BILLABLE`: what costs money, for example instances, RDS databases and manual
  snapshots, NAT gateways, Elastic IPs, interface endpoints, load balancers,
  EKS, enabled KMS keys, secrets, and log groups holding data.
- `FREE`: what doesn't, such as keys pending deletion, target groups and idle
  Lambda functions.
- `IGNORED`: entries in `config/account-sweep-ignore.txt`, which says why each
  is kept.

`--region` limits it to one region, and `--cost` adds this month's spend
without credits (one Cost Explorer call, $0.01; its data lags about a day).

## After a rebuild

The new ECR repositories hold only the bootstrap's `latest` images, and
Kubernetes starts from them. No earlier release tag is there, so re-running an
old `Deploy to EKS` run fails its ECR check. Build a release into the new
repositories first:

1. Re-run the `Docker Image CI` run for the commit at the tip of `main`, or
   push a commit to `main`. The new repositories have none of its tags, so the
   push goes through; the deploy skips older commits.
2. Approve the `Deploy to EKS` run that follows in the `production`
   environment. It moves the live Deployments to the new tag.

Check the rebuilt stack:

```bash
ansible-playbook ansible/playbooks/status.yml
export KUBECONFIG="$PWD/.generated/kubeconfig"
kubectl get ingress hospital-ingress -n hospitalsystem -o wide
dig +short app.hospitalsyst.cc CNAME
```

## Database

The database lives only as long as the stack. A destroy deletes the RDS
instance with its automated backups and takes no final snapshot, so nothing
keeps billing, and every new stack starts with an empty database. `seed.sql`,
a plain `pg_dump`, carries the data from one stack to the next. Git ignores it
because it holds user accounts and patient records.

**Loading it.** The bootstrap fills the new database
(`ansible/playbooks/database.yml`) after it has stored the connection string
and before the backend starts:

1. If the database has no tables and `seed.sql` is in the repository root, a
   short-lived `psql` pod loads it in one transaction. The password reaches the
   pod on stdin, so it's neither in the pod spec nor in the API server's audit
   log. A database that has tables is left alone, so a later bootstrap never
   loads it twice.
2. The migration Job applies the migrations of the image the backend runs, or
   is about to start from: on a seeded database the ones newer than the dump,
   and without `seed.sql` the whole schema with no rows. The application
   creates no first user, so without `seed.sql` nobody can sign in.

**Saving it.** The teardown writes `seed.sql` from the running database before
it destroys anything (`ansible/playbooks/dump-database.yml`, also runnable on
its own; see [Playbooks](operations.md#playbooks)):

- The database only accepts connections from the cluster (see
  [Database Access](operations.md#database-access)), so `pg_dump` runs in a
  short-lived pod with the same settings as the seed pod and gets the password
  on stdin.
- The SSM tunnel cuts long downloads from the cluster after 4 to 7 MB, so the
  dump comes over gzip'd, and a cut download is tried up to three times. 6 MB
  of demo data is 1.2 MB gzip'd. Uploads, such as loading `seed.sql`, aren't
  affected. A gzip'd dump of a few MB, roughly 20 MB of SQL, would need to be
  fetched in pieces.
- The dump replaces `seed.sql` only if `pg_dump` succeeded and the file ends
  like a complete dump. The old `seed.sql` is kept in the ignored
  `.generated/seed-backups/`, one timestamped file per save.
- A database without tables, from a bootstrap that failed before the seed,
  leaves `seed.sql` alone.
- If the dump fails, the teardown stops before it destroys anything. The dump
  needs the nodes: on a paused stack run `resume.yml` first. To destroy the
  database without saving it, run `./scripts/teardown.sh --skip-dump`.

**Restoring.** RDS keeps a day of automated backups while the stack is up, but
a point-in-time restore creates a new instance outside Terraform. Start over
from `seed.sql` instead: rebuild the stack, or scale the backend to 0, drop and
recreate the `hospital` database, and run `database.yml` (see
[Playbooks](operations.md#playbooks)).

The application's always-on demo on Render still uses its own Neon database,
so it shares no data with this stack.
