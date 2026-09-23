variable "aws_region" {
  description = "AWS region where the HospitalSystem infrastructure is deployed."
  type        = string
  default     = "eu-north-1"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "Project name used for common tags."
  type        = string
  default     = "hospitalsystem"
}

variable "eks_version" {
  description = "Kubernetes version of the EKS control plane. EKS upgrades one minor version at a time; see the README for the order."
  type        = string
  default     = "1.36"
}

variable "github_repository" {
  description = "GitHub repository allowed to assume deployment roles through OIDC."
  type        = string
  default     = "KaanMyumyun/HospitalSystem"
}

variable "github_deploy_branch" {
  description = "Only workflows running on this branch of github_repository can assume the ECR push role."
  type        = string
  default     = "main"
}

variable "github_deploy_environment" {
  description = "Only jobs using this GitHub environment of github_repository can assume the EKS deploy role. Limit the environment to github_deploy_branch in the repository settings."
  type        = string
  default     = "production"
}

variable "github_oidc_provider_arn" {
  description = "ARN of an existing token.actions.githubusercontent.com OIDC provider in this account. An account can hold only one, so set this when another project already created it. Empty creates it here."
  type        = string
  default     = ""
}

variable "app_domain_name" {
  description = "Public application domain name."
  type        = string
  default     = "app.hospitalsyst.cc"
}

variable "cloudflare_zone_name" {
  description = "Cloudflare DNS zone used for ACM validation and the public app CNAME."
  type        = string
  default     = "hospitalsyst.cc"
}

variable "run_ansible_bootstrap" {
  description = "Run the local Ansible bootstrap after Terraform creates or updates the EKS infrastructure."
  type        = bool
  default     = true
}

variable "push_initial_ecr_images" {
  description = "Build and push initial backend/frontend images from local source before Kubernetes bootstrap."
  type        = bool
  default     = true
}

variable "backend_source_dir" {
  description = "Backend application source directory. Empty defaults to the HospitalSystem repo checked out beside this one."
  type        = string
  default     = ""
}

variable "frontend_source_dir" {
  description = "Frontend application source directory. Empty defaults to hospital-frontend inside the backend source directory."
  type        = string
  default     = ""
}

variable "backend_dockerfile" {
  description = "Backend Dockerfile path relative to backend_source_dir."
  type        = string
  default     = "HospitalSystem/Dockerfile"
}

variable "frontend_dockerfile" {
  description = "Frontend Dockerfile path relative to frontend_source_dir."
  type        = string
  default     = "Dockerfile"
}

variable "initial_image_tag" {
  description = "Image tag used by first-push bootstrap and initial Kubernetes manifests."
  type        = string
  default     = "latest"
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token. Leave empty to let the provider read CLOUDFLARE_API_TOKEN from the environment."
  type        = string
  sensitive   = true
  default     = ""
}
