# CI/CD Workflow

![CI/CD pipeline](../assets/images/cicd-pipeline.png)

The CI/CD pipeline lives in the `.github/workflows` directory of the application
repository:

[KaanMyumyun/HospitalSystem](https://github.com/KaanMyumyun/HospitalSystem)

A copy is also kept in this repository under `CICD/.github/workflows/` for
documentation and review. Because it is not at the repository root
`.github/workflows/`, it does not run from this infrastructure repository.

The workflows are chained so deployment only happens after build and test
success:

1. `CI`
   - runs on pull requests, pushes to `main`, and weekly (Mondays 03:00 UTC)
     so published images pick up base image security fixes
   - restores, builds and tests the backend
   - audits npm dependencies (fails on `high` or worse), then lints, tests and
     builds the frontend

2. `Docker Image CI`
   - runs after the `CI` workflow succeeds on a push to `main` or on the weekly
     scheduled run
   - checks out the exact commit that passed CI
   - builds backend and frontend Docker images once each with Buildx, reusing
     build-stage layers from the GitHub Actions cache. The runtime stage is
     always rebuilt, so its `apk upgrade` picks up Alpine security fixes on
     every build; the weekly run skips the cache altogether. The frontend is
     built with `VITE_API_URL=/api`, so it calls the backend on whatever host
     serves it and the same image works under any domain.
   - scans those images with Trivy; a fixable `HIGH` or `CRITICAL`
     vulnerability stops the run before anything is pushed, so the pushed
     images are the scanned ones
   - uses GitHub Actions OIDC to assume the AWS ECR push role
   - pushes each image to both Docker Hub and Amazon ECR
   - saves the pushed tag and commit as a `release` artifact for the deploy

Images are tagged two ways:

| Tag | Example | Why it exists |
| --- | ------- | ------------- |
| `latest` | `hospital-backend:latest` | Convenience tag for manual testing and simple local references. |
| date + short SHA + run number + attempt | `2026-08-21-a1b2c3d-57-1` | The tag the EKS deploy workflow uses. It is unique to the build, so the weekly rebuild of an unchanged commit gets a new tag instead of overwriting the image Kubernetes runs. Tags from before the attempt was added end at the run number. |

In ECR every tag except `latest` is immutable (`terraform/ecr.tf`): a push
that reuses a tag fails instead of replacing the image, so the tag a deploy
checked and the tag a rollback returns to always name the same image. A
re-run of a `Docker Image CI` run keeps its run number but not its attempt,
so it pushes under a new tag instead of failing on the one the first attempt
pushed.

3. `Deploy to EKS`
   - runs after the Docker image workflow succeeds
   - waits for a reviewer to approve it in the `production` GitHub environment
   - reads the tag and commit from the Docker run's `release` artifact
     instead of working the tag out again, and skips the deploy if that commit
     is no longer the tip of `main`
   - assumes the AWS EKS deployment role through OIDC
   - finds the running ops instance and sends it the deploy SSM document with
     that tag
   - on the instance, the document runs `scripts/deploy-release.py`: it checks
     that both images exist in ECR, sets the backend and frontend Deployment
     images, waits for both rollouts, and smoke tests both apps. If a rollout
     or the smoke test fails, it puts both Deployments back on their previous
     images
   - fails unless the SSM command succeeded, and prints its output

The Kubernetes API endpoint is private, so GitHub-hosted runners can't reach
it. The deploy role may only send the `hospitalsystem-deploy` document to the
ops instance. The document's only input is the image tag: it builds the image
URLs from the ECR repositories itself, and the admission policy still rejects
any other image. A self-hosted runner inside the VPC was ruled out because the
application repository is public, and pull requests from forks could run
workflows on it.

If the app is scaled down to `0`, the deploy still updates the Deployment image
fields to the new tag. It skips waiting for a rollout because
no pods are running. The next manual scale-up starts pods from that exact image
tag.

Before it changes anything, the script looks up both tags in ECR, through an
ECR API VPC endpoint because the ops instance has no internet access. A
missing image fails the deploy with both apps untouched. After both rollouts
finish, it requests the backend's `/health/ready`, which also checks the
database, and the frontend's index page. These requests go through the
Kubernetes API server's Service proxy, not the ALB, so they don't test the
Ingress, ALB or DNS. If a rollout fails or times out, or a smoke request
still fails after three tries, the script sets both Deployments back to the
images they had before the deploy and waits for that rollout. Both apps go
back even when only one failed, so they never run different releases. The
deploy still fails, and its output says whether the rollback finished or
which images to restore by hand. Rollout, smoke test and rollback fit within
the document's 600-second timeout.

Both Deployments roll out with `RollingUpdate` (`maxSurge: 1`,
`maxUnavailable: 0`): a new pod starts next to the old one, and the old pod is
removed only once the new one is available. The namespace has the
`elbv2.k8s.aws/pod-readiness-gate-inject: enabled` label, so the AWS Load
Balancer Controller holds a new pod's readiness until its ALB target passes
health checks. A terminating pod waits 15 seconds in a `preStop` hook while the
ALB deregisters it, and the target groups drain for 30 seconds. If the new pod
never becomes healthy, the old pod keeps serving, the Deployment is marked
failed after `progressDeadlineSeconds: 300`, and the deploy rolls both apps
back and fails.

The backend first checks `/health/ready` with a startup probe, giving the app
and the database about two minutes to become reachable. Kubernetes holds
container readiness until this succeeds, so each new pod must pass a database
check before its rollout can complete. The Load Balancer Controller waits
for container readiness before registering the pod as an ALB target.

After that first successful check, the startup probe stops. Recurring readiness,
liveness, and ALB checks use `/health`, which does not query the database, so
a database restart, such as RDS installing a minor version, doesn't take every
backend pod out of the ALB or restart them all at once. A later database
outage therefore doesn't make an already-started pod unready;
`./scripts/monitoring.sh app` checks the database on demand.

The ops instance's IAM role gets Kubernetes access through a Terraform-managed
EKS access entry in the `hospitalsystem:deployers` group. That group is bound
by `kubernetes/rbac/github-actions-deploy.yaml.j2` to read and patch only the
`hospital-backend` and `hospital-frontend` Deployments, and to send GET
requests to the two app Services for the deploy's smoke test.
`kubernetes/policy/deploy-image-only.yaml.j2` then rejects any change by the
group other than container images. The GitHub Actions deploy role has no
Kubernetes access: it only sends the SSM deploy document to the ops instance.

The cluster keeps `API_AND_CONFIG_MAP` authentication so existing node and
other `aws-auth` mappings remain valid. Bootstrap no longer creates or patches
that ConfigMap. Leave the ConfigMap and its node mappings in place:
existing managed node groups can still depend on them. EKS manages access for
new managed node groups. See the AWS guidance on
[migrating access entries](https://docs.aws.amazon.com/eks/latest/userguide/migrating-access-entries.html)
and [managed node group access](https://docs.aws.amazon.com/eks/latest/userguide/creating-access-entries.html).

The GitHub Actions roles trust only `KaanMyumyun/HospitalSystem`, each with one
OIDC subject:

- The ECR push role trusts workflows running on `main` (`github_deploy_branch`
  in Terraform). Docker Image CI qualifies because `workflow_run` jobs run on
  the default branch. Protect `main` in GitHub: require pull requests and
  block force pushes and deletion.
- The EKS deploy role trusts jobs that use the `production` environment
  (`github_deploy_environment`). A job with an environment gets a token whose
  subject is the environment, not the branch, so GitHub has to enforce the
  branch. In Settings, Environments, `production`, set Deployment branches to
  `main` only and add required reviewers, so every deploy waits for approval.

An AWS account can hold only one `token.actions.githubusercontent.com` OIDC
provider. Terraform creates it unless `github_oidc_provider_arn` is set; set it
to the existing provider's ARN when another project in the account already has
one.

The workflows pin every action to a commit SHA, and Dependabot keeps the pins
current. The deploy workflow reads the SSM document and ops instance name from
the `DEPLOY_SSM_DOCUMENT` and `DEPLOY_INSTANCE_NAME` repository variables (the
`deploy_ssm_document_name` and `ops_instance_name` Terraform outputs); the
application README lists all the variables the workflows need.

## Releases with migrations

A deploy only changes images; it never touches the database schema. The deploy
group can't create Jobs or read the backend Secret, and the database accepts
connections only from inside the cluster. When a release adds an EF Core
migration, apply it before approving that release's `Deploy to EKS` run:

```bash
ansible-playbook ansible/playbooks/migrate.yml -e image_tag=2026-08-22-8f88481-57-1
```

The playbook checks that the tag is in ECR, runs the image's `--migrate`
command as a Job in the cluster (`kubernetes/backend/migrate-job.yaml.j2`)
with only the connection string from the backend Secret, prints the Job's log
and deletes it. For a release without new migrations it changes nothing.
Migrations only go forward, and the running release keeps serving while they
apply, so each one has to work with both the running image and the new one;
the application README describes how to write them that way.
