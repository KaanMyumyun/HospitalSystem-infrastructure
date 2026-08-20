# HospitalSystem Infrastructure

Infrastructure and Kubernetes deployment configuration for:

[KaanMyumyun/HospitalSystem](https://github.com/KaanMyumyun/HospitalSystem)

Current AWS deployment target:

- Public app URL: `https://app.hospitalsyst.cc`
- Runtime platform: Amazon EKS
- Container registry: Amazon ECR
- Ingress: AWS Application Load Balancer
- HTTPS: AWS Certificate Manager
- Database: Neon PostgreSQL
- Frontend API base: `/api`

## Repository Contents

```text
terraform/
├── *.tf
└── imports/        # ignored unedited Terracognita output
kubernetes/
├── namespace.yaml
├── backend/
│   ├── configmap.yaml
│   ├── deployment.yaml
│   └── service.yaml
├── frontend/
│   ├── deployment.yaml
│   └── service.yaml
└── ingress/
    └── ingress.yaml
```

The Kubernetes manifests define:

- `hospitalsystem` namespace
- backend Deployment and Service
- frontend Deployment and Service
- ALB-backed Ingress for `app.hospitalsyst.cc`
- HTTPS listener using ACM
- `/api` routing to the backend
- `/` routing to the frontend

The Terraform configuration defines the AWS infrastructure. The existing AWS
resources were reverse engineered with Terracognita, then split and cleaned into
readable Terraform files. The raw Terracognita output is kept under
`terraform/imports/` locally and ignored by Git.

## Operational Notes

The cluster is intentionally run in a low-cost mode while not testing:

- app Deployments can be scaled to `0`
- EKS managed node group can be scaled to `desiredSize=0`
- EKS control plane remains active while the cluster exists

The app currently uses `latest` image tags with `imagePullPolicy: Always`.
GitHub Actions in the app repository builds and pushes the images to ECR.

## Roadmap

Planned infrastructure improvements:

- Import the existing AWS resources into the cleaned Terraform state and review plans before applying changes.
- Continue using Terraform for VPC, subnets, route tables, security groups, EKS, node groups, IAM, ECR, ACM, and DNS-related infrastructure where practical.
- Use Ansible for deployment orchestration where it provides value, such as applying Kubernetes manifests, installing Helm charts, waiting for Ingress readiness, and updating DNS records.
- Replace `latest`-based Kubernetes deployments with immutable Git SHA image tags for safer rollbacks.
- Improve rollout behavior once capacity allows it by moving from `Recreate` to `RollingUpdate`.
- Add monitoring/logging in a cost-aware way after the deployment path is stable.

## Repository Boundaries

This repository is intended to contain reusable infrastructure source only.

Committed:

- Kubernetes manifests
- infrastructure README
- future Terraform and Ansible source
