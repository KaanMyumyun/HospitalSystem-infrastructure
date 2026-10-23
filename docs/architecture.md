# Architecture

The app runs on Amazon EKS in a private network, behind an AWS Application Load
Balancer, with its database on Amazon RDS. Terraform builds the AWS layer, and
Ansible applies the Kubernetes layer on top of it.

![AWS infrastructure architecture](../assets/images/infrastructure-architecture.png)

## Traffic flow

```text
User
  -> https://app.hospitalsyst.cc
  -> AWS Application Load Balancer in public subnets
  -> Kubernetes Ingress managed by AWS Load Balancer Controller
  -> frontend service for /
  -> backend service for /api
  -> backend connects to RDS PostgreSQL in the private subnets
```

## AWS Resources

| Area | Resources | Purpose |
| ---- | --------- | ------- |
| Network | VPC `kubes` with CIDR `10.0.0.0/16`, and the security groups of the ops instance, the VPC endpoints and the database | Isolated AWS network for the EKS deployment. |
| Public subnets | `p1` `10.0.0.0/20` in `eu-north-1a`, `p2` `10.0.16.0/20` in `eu-north-1b` | Host internet-facing resources such as the ALB and NAT gateways. Tagged for Kubernetes external load balancers. |
| Private subnets | `private1` `10.0.32.0/20` in `eu-north-1a`, `private2` `10.0.48.0/20` in `eu-north-1b` | Host EKS worker nodes and application pods away from direct public internet exposure. Tagged for internal Kubernetes load balancers. |
| Routing | Internet gateway, public route table, private route tables, NAT gateways, Elastic IPs, and VPC endpoints for SSM, S3 and the ECR API | Public subnets route through the internet gateway. Private subnets route outbound traffic through NAT gateways, and S3 traffic through the free gateway endpoint. The ECR API endpoint serves only the deploy's image check from the ops instance. |
| Compute | Amazon EKS cluster `eks-pr1`, managed node group `hospitalsystempr1`, Amazon Linux 2023 worker nodes | Runs the Kubernetes control plane and worker capacity for the app. The node group is sized for low-cost testing and can scale to zero. The Kubernetes API endpoint is private. |
| Cluster access | EC2 instance `hospitalsystem-ops` in a private subnet, SSM document `hospitalsystem-deploy`, EKS access entry | Accepts no inbound connections and has no internet access. Operators reach the private API through an SSM port-forwarding session on it, and GitHub Actions deploys by sending it the SSM document. |
| Database | RDS for PostgreSQL 18 instance `hospitalsystem-db` (`db.t4g.micro`, 20 GB gp2, Single-AZ), its DB subnet group, parameter group and security group | Not public and encrypted with the project's KMS key. Accepts connections on 5432 only from the EKS cluster security group, which the nodes and pods use. Lives as long as the stack. |
| Containers | Amazon ECR repositories for `hospital-backend` and `hospital-frontend` | Store the images built by GitHub Actions; a lifecycle policy keeps the latest 30 of each. |
| Ingress | AWS Application Load Balancer, HTTP/HTTPS listeners, target groups, and AWS Load Balancer Controller | Exposes the app publicly and maps Kubernetes Ingress rules to AWS load-balancing resources. |
| TLS | AWS Certificate Manager certificate, validated through a Cloudflare DNS record | Provides HTTPS for `app.hospitalsyst.cc` using TLS 1.2/1.3; HTTP traffic redirects to HTTPS. |
| Identity | IAM roles and policies for EKS, worker nodes, AWS Load Balancer Controller, GitHub Actions OIDC, ECR push, and EKS deployment; the EKS and GitHub Actions OIDC providers | Allows AWS services, Kubernetes components, and CI/CD workflows to use AWS resources without long-lived access keys. |
| Encryption | KMS key `alias/hospitalsystem`, rotated automatically | Encrypts ECR, Kubernetes Secrets, Secrets Manager, CloudWatch Logs, RDS and the alert topic. |
| Secrets | Secrets Manager secret `hospitalsystem/backend` | Holds the backend's connection string and JWT key. |
| Logs | VPC flow logs and the EKS control plane log group, in CloudWatch Logs | Kept for 365 days. |
| Alerts | SNS topic `hospitalsystem-alerts` and the node group alarm | Email alarm changes (see [Monitoring](monitoring.md#alarms)). |

Terraform manages all of these except the ALB, its listeners, target groups and
security groups, which the AWS Load Balancer Controller creates from the
Ingress. A few things to know about what it manages:

- It creates the backend's Secrets Manager secret but never its value, and the
  RDS instance but not its data or its password, which is write-only.
- The vpc-cni, coredns and kube-proxy EKS add-ons run at EKS's default version
  for `eks_version`; a later apply also follows AWS when it changes that
  default.
- It creates the GitHub Actions OIDC provider unless `github_oidc_provider_arn`
  names an existing one.
- It also runs the Ansible bootstrap after an apply and the Kubernetes cleanup
  before a destroy.

**Email records.** The domain sends and receives no email. Three records in the
`hospitalsyst.cc` Cloudflare zone say so, and tell mail servers to reject mail
that claims to come from it:

| Record | Value |
| ------ | ----- |
| TXT `hospitalsyst.cc` | `v=spf1 -all` |
| TXT `_dmarc.hospitalsyst.cc` | `v=DMARC1; p=reject;` |
| MX `hospitalsyst.cc` | `0 .` (a null MX) |

They were added by hand, outside Terraform, so the teardown leaves them alone. A
new zone needs them added again.

## Kubernetes layer

Ansible renders the templates in `kubernetes/` and applies them (see
[Applying the manifests](operations.md#applying-the-manifests)).

**Workloads**

- Backend and frontend Deployments and Services, two replicas each, one per
  node, with a PodDisruptionBudget that keeps one available during a drain.
  HorizontalPodAutoscalers own the replica count, which is fixed at two for now
  (see [Pause and scaling](operations.md#pause-and-scaling)).
- An ExternalSecret that copies the backend secret from AWS Secrets Manager.
- A Job that runs the backend image's database migrations, created by the
  bootstrap and by `migrate.yml` (see
  [Database](disaster-recovery.md#database)), never by a deploy.

**Routing**

- An ALB-backed Ingress for `app.hospitalsyst.cc`: `/api` to the backend, `/`
  to the frontend.
- The ALB appends the client's address to `X-Forwarded-For`. The API reads it
  from there only on requests from the public subnets, which its ConfigMap
  lists as `ForwardedHeaders__KnownNetworks__*`, so its rate limits count each
  client separately.
- An HTTPS listener using ACM, which adds `Strict-Transport-Security` (one
  year), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and a
  Content-Security-Policy to every response, and drops the ALB's own `Server`
  header. The policy only allows the app's own origin
  (`default-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`),
  so a frontend build that calls an API on another host would need it widened.

**Security**

- The `hospitalsystem` namespace enforces the restricted Pod Security Standard.
  Both apps run as non-root with a read-only root filesystem (emptyDirs for
  `/tmp` and the backend's `.aspnet` keys), no capabilities, the runtime's
  default seccomp profile and no service account token.
- NetworkPolicies deny all ingress to the namespace except port 8080 from the
  ALB's public subnets and from the EKS control plane's network interfaces (the
  deploy's smoke test goes through the API server), plus the backend's metrics
  port 9091 from the control plane only, for `scripts/monitoring.sh`'s per-pod
  metrics read. The vpc-cni add-on enforces them; the node's own traffic, such
  as kubelet probes, is always allowed.
- Namespace-scoped RBAC lets the ops instance's deploy group change the app
  Deployments' images. GitHub Actions reaches it only through the SSM deploy
  document (see [CI/CD Workflow](cicd.md#cicd-workflow)).
- An admission policy lets the app Deployments run only this account's backend
  and frontend ECR images with their own entrypoint, so the deploy role can
  change which release runs but not what code runs.
- A second admission policy lets the deploy group change only container
  images, so a deploy can't add a lifecycle hook, probe, environment variable
  or volume either.

## Database schema

![HospitalSystem database schema](../assets/images/database-schema.png)

The application's EF Core migrations create this schema. This repository only
runs them (see [Database](disaster-recovery.md#database)) and never changes the
schema itself. Every foreign key is `ON DELETE RESTRICT`, so a row that others
reference can't be deleted until those references are gone.

## Repository Contents

```text
terraform/
├── *.tf            # the root module (see Layout and sizing)
├── modules/
│   ├── network/    # VPC, subnets, NAT gateways, VPC endpoints, flow logs
│   └── eks/        # cluster, node group, add-ons and their IAM roles
└── bootstrap/      # the S3 state bucket, created and deleted by the scripts
ansible/
├── group_vars/all/
│   ├── main.yml        # Ansible-owned settings
│   └── terraform.yml   # generated by Terraform, ignored
├── playbooks/
└── tasks/
    ├── kubeconfig.yml  # shared kubeconfig and SSM tunnel setup
    ├── migrate.yml     # the migration Job, used by database.yml and migrate.yml
    └── alb-alarm.yml   # one ALB alarm, used by monitoring.yml
scripts/
├── tf.sh                       # Terraform wrapper: loads .env.local, manages the state bucket
├── teardown.sh                 # full destroy, cleanup and account sweep
├── pre-destroy-cleanup.sh      # deletes the ALB and what the controller made
├── cleanup-orphans.sh          # deletes what a destroy leaves behind
├── account-sweep.py            # lists what is left in every region
├── monitoring.sh               # health checks across AWS, Kubernetes and GitHub
├── rollout-check.sh            # checks a deploy replaces pods without dropping requests
├── image-check.sh              # which image and commit each app runs
├── deploy-release.py           # the deploy the SSM document runs on the ops instance
├── rotate-jwt-key.py           # generates a new JWT key and pushes it
├── push-initial-ecr-images.sh  # run by Terraform on apply
├── apply-workload.py           # used by apply-kubernetes.yml
├── backend-secret.py           # used by backend-secret.yml and apply-kubernetes.yml
├── read-config.py              # used through lib/config.sh
└── lib/                        # shared shell functions
config/
├── alb-alarms.json             # the ALB alarms, read by monitoring.yml and the cleanup
└── account-sweep-ignore.txt    # resources the sweep reports as IGNORED
tests/                  # Python regression tests (see Checks and Tests)
assets/images/          # README diagrams: each .png is rendered from its .svg
CICD/                   # copy of the app repository's workflows, not run here
.github/                # this repository's CI and Dependabot
docs/                   # the documentation the README links to
coursework/             # university coursework describing this project, in Bulgarian
kubernetes/             # Jinja templates rendered by apply-kubernetes.yml
├── namespace.yaml.j2
├── rbac/
│   └── github-actions-deploy.yaml.j2
├── policy/
│   ├── trusted-workloads.yaml.j2
│   ├── deploy-image-only.yaml.j2
│   └── network.yaml.j2
├── backend/
│   ├── configmap.yaml.j2
│   ├── deployment.yaml.j2
│   ├── external-secret.yaml.j2  # applied by backend-secret.yml
│   ├── migrate-job.yaml.j2      # created by tasks/migrate.yml
│   ├── hpa.yaml.j2
│   ├── pdb.yaml.j2
│   └── service.yaml.j2
├── frontend/
│   ├── deployment.yaml.j2
│   ├── hpa.yaml.j2
│   ├── pdb.yaml.j2
│   └── service.yaml.j2
└── ingress/
    └── ingress.yaml.j2
```
