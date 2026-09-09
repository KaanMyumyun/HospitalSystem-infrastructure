# Portable One-Apply AWS Rebuild Plan

## Goal

Make the HospitalSystem AWS infrastructure portable to a new AWS account so a fresh account can run:

```bash
cd terraform
terraform apply
```

and bring up the AWS/EKS application stack automatically.

After implementation, Terraform should:

1. Detect the active AWS account ID.
2. Create the AWS network, IAM, EKS, and node group resources.
3. Create ECR repositories for backend and frontend images.
4. Build and push the first backend/frontend images from the local application source on this PC.
5. Create GitHub Actions OIDC provider and deploy/push roles.
6. Request an ACM certificate for `app.hospitalsyst.cc`.
7. Create the ACM DNS validation record in Cloudflare automatically.
8. Wait until ACM issues the certificate.
9. Run the existing Ansible bootstrap.
10. Apply Kubernetes manifests using the new account's ECR image URLs and ACM certificate ARN.
11. Let the AWS Load Balancer Controller create the ALB, listeners, target groups, and ALB security groups.
12. Update the Cloudflare app DNS record to point `app.hospitalsyst.cc` to the new ALB hostname.

The external database remains outside AWS/Terraform and is supplied through `HOSPITALSYSTEM_CONNECTION_STRING`.

## Current Problem

The current repository is not portable to a new AWS account because it contains hardcoded values from the current account:

- AWS account ID: `147914447694`
- ECR image URLs:
  - `147914447694.dkr.ecr.eu-north-1.amazonaws.com/hospital-backend:latest`
  - `147914447694.dkr.ecr.eu-north-1.amazonaws.com/hospital-frontend:latest`
- ACM certificate ARN:
  - `arn:aws:acm:eu-north-1:147914447694:certificate/f82d2036-d650-45a0-bd19-3e67ccc16e39`
- IAM role ARNs in Ansible variables.

Important AWS resources also currently exist outside Terraform:

- ECR repositories:
  - `hospital-backend`
  - `hospital-frontend`
- GitHub Actions OIDC provider:
  - `token.actions.githubusercontent.com`
- ACM certificate for:
  - `app.hospitalsyst.cc`
- CloudWatch alarms created by Ansible/CLI.

Kubernetes can apply manifests before images exist, but pods will not become healthy until ECR contains pullable images for `:latest`. The agreed approach is to have Terraform run a local script automatically during apply to build and push the first real backend/frontend images before Kubernetes bootstrap runs.

## Scope

### Terraform Should Manage

- VPC, subnets, route tables, internet gateways, NAT gateways, EIPs.
- EKS cluster and managed node group.
- IAM roles, policies, role policies, and role attachments.
- EKS OIDC provider for service accounts.
- GitHub Actions OIDC provider.
- ECR backend/frontend repositories.
- ACM certificate request and validation wait.
- Cloudflare ACM DNS validation record.
- Stable CloudWatch alarms where practical.
- Local first-image push orchestration through `terraform_data` and `local-exec`.

### Terraform Should Not Directly Manage

These are created by AWS services or Kubernetes controllers and should remain indirectly managed:

- ALB `hospital-system-alb`.
- ALB listeners.
- ALB target groups.
- ALB/controller-created security groups.
- EKS-created cluster security group.
- EKS managed node group Auto Scaling Group.
- EKS launch template.
- NAT, EKS, and ALB network interfaces.

Terraform manages the parent resources and Kubernetes Ingress/controller behavior. Directly importing these generated child resources would create ownership conflicts or brittle state.

### Outside AWS/Terraform

- Cloudflare zone ownership remains outside AWS, but Terraform will use the Cloudflare provider to create DNS records through `CLOUDFLARE_API_TOKEN`.
- Neon/PostgreSQL database remains external.
- GitHub repository remains external.
- GitHub Actions workflows remain in the application repository, not this infra repo, unless separately requested.

## Implementation Details

### 1. Replace Hardcoded AWS Account ID

Add account discovery:

```hcl
data "aws_caller_identity" "current" {}
```

Update locals:

```hcl
locals {
  account_id = data.aws_caller_identity.current.account_id
}
```

Replace all hardcoded `147914447694` usage in Terraform-generated values with `local.account_id`.

Files/classes of values to update:

- Terraform locals and IAM policy resource ARNs.
- ECR repository URLs.
- Ansible variables.
- Kubernetes deployment image references.
- Kubernetes Ingress ACM certificate annotation.

The old account ID should not appear in generated runtime configuration after this change.

### 2. Add Terraform-Managed ECR

Create a new Terraform file, for example `terraform/ecr.tf`.

Resources:

```hcl
resource "aws_ecr_repository" "backend" {
  name         = "hospital-backend"
  force_delete = true
}

resource "aws_ecr_repository" "frontend" {
  name         = "hospital-frontend"
  force_delete = true
}
```

Add lifecycle policies to keep recent images, for example the latest 10 tagged images.

Use repository ARNs in the GitHub Actions ECR push role policy instead of hand-built ARN strings where possible.

Expected outputs:

- `account_id`
- `ecr_registry`
- `backend_repository_url`
- `frontend_repository_url`

Example:

```hcl
output "ecr_registry" {
  value = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
}

output "backend_repository_url" {
  value = aws_ecr_repository.backend.repository_url
}

output "frontend_repository_url" {
  value = aws_ecr_repository.frontend.repository_url
}
```

### 3. Add Automatic First Image Push

Create a local script:

```text
scripts/push-initial-ecr-images.sh
```

Default local source paths discovered on this PC:

- Backend: `/home/kaan/HospitalSystem`
- Frontend: `/home/kaan/HospitalSystem/hospital-frontend`

Add Terraform variables:

```hcl
variable "push_initial_ecr_images" {
  type    = bool
  default = true
}

variable "backend_source_dir" {
  type    = string
  default = "/home/kaan/HospitalSystem"
}

variable "frontend_source_dir" {
  type    = string
  default = "/home/kaan/HospitalSystem/hospital-frontend"
}

variable "backend_dockerfile" {
  type    = string
  default = "HospitalSystem/Dockerfile"
}

variable "frontend_dockerfile" {
  type    = string
  default = "Dockerfile"
}

variable "initial_image_tag" {
  type    = string
  default = "latest"
}
```

Script behavior:

1. Fail fast if `docker` is missing.
2. Fail fast if `aws` is missing.
3. Fail fast if Docker daemon is unavailable.
4. Fail fast if backend or frontend source directory does not exist.
5. Fail fast if either Dockerfile does not exist.
6. Log in to ECR:

   ```bash
   aws ecr get-login-password --region "$AWS_REGION" \
     | docker login --username AWS --password-stdin "$ECR_REGISTRY"
   ```

7. Build backend image from `$BACKEND_SOURCE_DIR`.
8. Build frontend image from `$FRONTEND_SOURCE_DIR`.
9. Tag backend as:

   ```text
   $BACKEND_REPOSITORY_URL:$IMAGE_TAG
   ```

10. Tag frontend as:

   ```text
   $FRONTEND_REPOSITORY_URL:$IMAGE_TAG
   ```

11. Push both images.

Add a Terraform resource similar to:

```hcl
resource "terraform_data" "initial_ecr_image_push" {
  count = var.push_initial_ecr_images ? 1 : 0

  input = {
    script_sha              = filesha256("${path.module}/../scripts/push-initial-ecr-images.sh")
    backend_source_dir      = var.backend_source_dir
    frontend_source_dir     = var.frontend_source_dir
    backend_repository_url  = aws_ecr_repository.backend.repository_url
    frontend_repository_url = aws_ecr_repository.frontend.repository_url
    image_tag               = var.initial_image_tag
  }

  provisioner "local-exec" {
    working_dir = "${path.module}/.."
    interpreter = ["/bin/bash", "-c"]
    command     = "${path.module}/../scripts/push-initial-ecr-images.sh"

    environment = {
      AWS_REGION              = var.aws_region
      ECR_REGISTRY            = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
      BACKEND_SOURCE_DIR      = var.backend_source_dir
      FRONTEND_SOURCE_DIR     = var.frontend_source_dir
      BACKEND_DOCKERFILE      = var.backend_dockerfile
      FRONTEND_DOCKERFILE     = var.frontend_dockerfile
      BACKEND_REPOSITORY_URL  = aws_ecr_repository.backend.repository_url
      FRONTEND_REPOSITORY_URL = aws_ecr_repository.frontend.repository_url
      IMAGE_TAG               = var.initial_image_tag
    }
  }

  depends_on = [
    aws_ecr_repository.backend,
    aws_ecr_repository.frontend
  ]
}
```

The push should run automatically by default. Normal future applies can disable it:

```bash
terraform apply -var="push_initial_ecr_images=false"
```

### 4. Ensure Image Push Runs Before Kubernetes Bootstrap

Current Terraform runs Ansible through:

```hcl
resource "terraform_data" "ansible_bootstrap" {
  count = var.run_ansible_bootstrap ? 1 : 0
}
```

Update its dependencies so bootstrap waits for the initial image push when enabled.

Because Terraform cannot reference a counted resource directly unless indexed, use a safe dependency expression or split the bootstrap orchestration so the dependency is deterministic.

Accepted implementation approaches:

- Use separate `terraform_data` resources for enabled/disabled image push paths.
- Or make `initial_ecr_image_push` always exist and make the script no-op when disabled.

Recommended: make `terraform_data.initial_ecr_image_push` always exist and pass `PUSH_INITIAL_ECR_IMAGES=true/false`. This avoids count-index dependency complexity.

Then:

```hcl
depends_on = [
  aws_eks_cluster.main,
  aws_eks_node_group.hospitalsystempr1,
  aws_iam_role.eks_deploy_hospitalsystem,
  terraform_data.initial_ecr_image_push
]
```

### 5. Add GitHub Actions OIDC Provider

Create Terraform-managed provider:

```hcl
resource "aws_iam_openid_connect_provider" "github_actions" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = [
    "sts.amazonaws.com"
  ]

  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd"
  ]
}
```

Update `data.aws_iam_policy_document.github_actions_assume_role`:

```hcl
principals {
  type        = "Federated"
  identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
}
```

Keep the current repository restriction:

```hcl
"repo:${var.github_repository}:*"
```

In a new AWS account, no import is needed. Terraform creates it.

In the current AWS account, import before apply:

```bash
terraform import aws_iam_openid_connect_provider.github_actions arn:aws:iam::147914447694:oidc-provider/token.actions.githubusercontent.com
```

### 6. Add Terraform-Managed ACM Certificate

Create a Terraform-managed certificate:

```hcl
resource "aws_acm_certificate" "app" {
  domain_name       = var.app_domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}
```

Add variables:

```hcl
variable "app_domain_name" {
  type    = string
  default = "app.hospitalsyst.cc"
}

variable "cloudflare_zone_name" {
  type    = string
  default = "hospitalsyst.cc"
}
```

Output validation details for debugging:

- validation record name
- validation record type
- validation record value
- certificate ARN

### 7. Automate ACM DNS Validation In Cloudflare

Add Cloudflare provider to Terraform:

```hcl
terraform {
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}

provider "cloudflare" {}
```

The provider should read `CLOUDFLARE_API_TOKEN` from the environment.

Add zone lookup:

```hcl
data "cloudflare_zones" "app" {
  name = var.cloudflare_zone_name
}
```

Create the ACM validation CNAME:

```hcl
resource "cloudflare_dns_record" "acm_validation" {
  zone_id = data.cloudflare_zones.app.result[0].id
  name    = one(aws_acm_certificate.app.domain_validation_options).resource_record_name
  type    = one(aws_acm_certificate.app.domain_validation_options).resource_record_type
  content = one(aws_acm_certificate.app.domain_validation_options).resource_record_value
  ttl     = 60
  proxied = false
}
```

Add ACM validation wait:

```hcl
resource "aws_acm_certificate_validation" "app" {
  certificate_arn         = aws_acm_certificate.app.arn
  validation_record_fqdns = [cloudflare_dns_record.acm_validation.name]
}
```

Use the validated certificate ARN in the Kubernetes Ingress.

Important: Cloudflare provider syntax must be checked against the installed provider version before implementation. The current state references Cloudflare provider already, but Terraform config no longer declares it.

### 8. Generate Dynamic Kubernetes And Ansible Values

Current Kubernetes manifests hardcode account-specific ECR image URLs and ACM ARN.

Replace hardcoded Kubernetes manifests with Ansible templates or generated files.

Recommended approach:

- Keep source templates in `kubernetes/**/*.yaml.j2`.
- Ansible renders them to a generated ignored directory, for example `.generated/kubernetes/`.
- `apply-kubernetes.yml` applies generated manifests.

Template values:

- `backend_image`: `${backend_repository_url}:${initial_image_tag}`
- `frontend_image`: `${frontend_repository_url}:${initial_image_tag}`
- `acm_certificate_arn`: `aws_acm_certificate_validation.app.certificate_arn` or `aws_acm_certificate.app.arn`
- `app_domain_name`: `app.hospitalsyst.cc`
- GitHub deploy role ARN.
- EKS node role ARN.
- Load Balancer Controller role ARN.
- ECR registry URL.

Do not commit generated files unless the repo pattern requires it. Add generated output path to `.gitignore` if needed.

### 9. Pass Terraform Outputs Into Ansible

Current `ansible/group_vars/all.yml` contains hardcoded AWS account values.

Replace account-specific values with values passed from Terraform.

Recommended Terraform local-exec command:

```bash
ansible-playbook ansible/playbooks/bootstrap.yml \
  -e "aws_region=${var.aws_region}" \
  -e "eks_cluster_name=${local.cluster_name}" \
  -e "ecr_registry=${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com" \
  -e "backend_image=${aws_ecr_repository.backend.repository_url}:${var.initial_image_tag}" \
  -e "frontend_image=${aws_ecr_repository.frontend.repository_url}:${var.initial_image_tag}" \
  -e "github_actions_deploy_role_arn=${aws_iam_role.eks_deploy_hospitalsystem.arn}" \
  -e "eks_node_role_arn=${aws_iam_role.eks_node.arn}" \
  -e "load_balancer_controller_role_arn=${aws_iam_role.load_balancer_controller.arn}" \
  -e "acm_certificate_arn=${aws_acm_certificate.app.arn}" \
  -e "cloudflare_zone_name=${var.cloudflare_zone_name}" \
  -e "cloudflare_record_name=${var.app_domain_name}"
```

Keep non-account-specific defaults in `ansible/group_vars/all.yml`.

### 10. Cloudflare Final App DNS Record

The existing Ansible playbook `cloudflare-dns.yml` waits for the Kubernetes Ingress ALB hostname and creates/updates:

```text
app.hospitalsyst.cc -> <ALB DNS hostname>
```

Keep this behavior for now because the ALB is not known until the AWS Load Balancer Controller creates it from the Kubernetes Ingress.

Terraform will manage only the ACM validation CNAME. Ansible will manage the final app CNAME.

### 11. CloudWatch Alarms

Current monitoring playbook creates CloudWatch alarms using AWS CLI.

Options:

- Move stable alarm resources into Terraform.
- Keep ALB/target-group alarms in Ansible because target group names are controller-generated after Ingress creation.

Recommended first implementation:

- Move node group no-running-nodes alarm to Terraform.
- Keep ALB and target group alarms in Ansible until ALB discovery is made stable.
- Avoid duplicate creation of the same alarm from both Terraform and Ansible.

### 12. Update Documentation

Update README disaster recovery/new-account sections.

New required local environment:

```bash
export CLOUDFLARE_API_TOKEN='...'
export HOSPITALSYSTEM_CONNECTION_STRING='...'
export HOSPITALSYSTEM_JWT_SECRET='...'
```

Required local tools:

```text
docker
aws
kubectl
helm
ansible-playbook
terraform
```

New account flow:

```bash
aws sts get-caller-identity
cd terraform
terraform apply
```

Optional later apply without image rebuild:

```bash
terraform apply -var="push_initial_ecr_images=false"
```

## Existing Account Import Notes

These imports are only for adopting resources in the current AWS account. They are not needed in a brand-new AWS account.

```bash
terraform import aws_ecr_repository.backend hospital-backend
terraform import aws_ecr_repository.frontend hospital-frontend
terraform import aws_iam_openid_connect_provider.github_actions arn:aws:iam::147914447694:oidc-provider/token.actions.githubusercontent.com
```

For ACM in the current account, two choices exist:

- Import the existing issued certificate if staying in the current account.
- Or create a new Terraform-managed certificate and validate it through Cloudflare.

For the new AWS account migration, create a new Terraform-managed ACM certificate.

## Expected Final Apply Behavior

After implementation, this should work in a new AWS account:

```bash
export CLOUDFLARE_API_TOKEN='...'
export HOSPITALSYSTEM_CONNECTION_STRING='...'
export HOSPITALSYSTEM_JWT_SECRET='...'

cd terraform
terraform apply
```

Expected result:

- Terraform uses the new AWS account ID automatically.
- ECR repositories exist in the new account.
- Initial backend/frontend images are pushed to the new account's ECR.
- EKS cluster and nodes are created.
- GitHub OIDC and IAM roles are created for the new account.
- ACM cert for `app.hospitalsyst.cc` is requested and validated through Cloudflare.
- Kubernetes manifests deploy with the new ECR URLs and new ACM ARN.
- AWS Load Balancer Controller creates the ALB.
- Cloudflare app CNAME points to the ALB.
- The app is reachable over HTTPS if the external database connection string is valid.

## Test Plan

Run static checks:

```bash
cd terraform
terraform fmt -check
terraform validate
```

Run plan checks:

```bash
terraform plan
```

Confirm:

- No hardcoded `147914447694` remains in Terraform-generated runtime values.
- ECR repositories are created or imported.
- GitHub OIDC provider is managed by Terraform.
- ACM certificate and Cloudflare validation CNAME are planned.
- Kubernetes manifests use generated image URLs and certificate ARN.
- Terraform does not try to directly manage ALB, target groups, ALB security groups, ASG, launch template, or ENIs.

Run first apply in the target account:

```bash
terraform apply
```

Verify AWS:

```bash
aws sts get-caller-identity
aws ecr describe-repositories --region eu-north-1
aws ecr describe-images --region eu-north-1 --repository-name hospital-backend
aws ecr describe-images --region eu-north-1 --repository-name hospital-frontend
aws eks describe-cluster --region eu-north-1 --name eks-pr1
aws acm list-certificates --region eu-north-1
```

Verify Kubernetes:

```bash
aws eks update-kubeconfig --region eu-north-1 --name eks-pr1
kubectl get nodes
kubectl get pods -n hospitalsystem
kubectl get ingress hospital-ingress -n hospitalsystem -o wide
```

Verify DNS and HTTPS:

```bash
dig +short app.hospitalsyst.cc CNAME
curl -I https://app.hospitalsyst.cc
curl -I https://app.hospitalsyst.cc/api/health
```

Verify GitHub Actions later:

- Workflow has `permissions: id-token: write`.
- Workflow assumes the Terraform-created ECR push role.
- Workflow pushes new backend/frontend images to the new account's ECR.
- Workflow deploys to the new EKS cluster.

## Assumptions

- New AWS account credentials are active before running Terraform.
- Region remains `eu-north-1`.
- Cluster name remains `eks-pr1`.
- Domain remains `app.hospitalsyst.cc`.
- Cloudflare remains authoritative DNS for `hospitalsyst.cc`.
- `CLOUDFLARE_API_TOKEN` has `Zone:Read` and `DNS:Edit`.
- Backend source path remains `/home/kaan/HospitalSystem`.
- Frontend source path remains `/home/kaan/HospitalSystem/hospital-frontend`.
- Both source paths contain valid Dockerfiles.
- Docker is running locally during `terraform apply`.
- Backend database remains external and is supplied through `HOSPITALSYSTEM_CONNECTION_STRING`.
- Backend JWT secret is supplied through `HOSPITALSYSTEM_JWT_SECRET`.
- ALB remains Kubernetes-controller-owned through the existing Ingress model.
