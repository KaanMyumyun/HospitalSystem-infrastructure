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

variable "node_instance_types" {
  description = "EC2 instance types of the EKS node group."
  type        = list(string)
  default     = ["t3.small"]
}

variable "node_desired_size" {
  description = "Node count when the app is running. ansible/playbooks/resume.yml scales back to it after pause.yml scaled the node group to 0."
  type        = number
  default     = 2
}

variable "node_max_size" {
  description = "Most nodes the node group can have. Nothing adds nodes on its own, so this only caps what resume.yml or a manual scale sets."
  type        = number
  default     = 2

  validation {
    condition     = var.node_max_size >= var.node_desired_size
    error_message = "node_max_size must be at least node_desired_size."
  }
}

variable "db_instance_class" {
  description = "RDS instance class of the PostgreSQL database. Its memory sets max_connections (79 on db.t4g.micro), and the backend pods' pools in ansible/playbooks/backend-secret.yml have to fit under it."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_multi_az" {
  description = "Keep a standby copy of the database in a second availability zone, which RDS fails over to. Doubles the instance and storage cost."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  description = "Days of automated backups, for point-in-time restore while the stack is up. Destroy deletes them with the instance either way."
  type        = number
  default     = 1

  validation {
    condition     = var.db_backup_retention_days >= 1 && var.db_backup_retention_days <= 35
    error_message = "db_backup_retention_days must be between 1 and 35."
  }
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
  description = "Run the local Ansible bootstrap after Terraform creates or updates the EKS infrastructure. The Kubernetes cleanup on destroy runs either way."
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

variable "alert_email" {
  description = "Email address subscribed to the CloudWatch alarm topic. scripts/tf.sh sets it from ALERT_EMAIL; empty creates the topic without a subscription."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^$|^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.alert_email))
    error_message = "alert_email must be an email address or empty."
  }
}

variable "db_password" {
  description = "Master password of the PostgreSQL instance. scripts/tf.sh sets it from HOSPITALSYSTEM_DB_PASSWORD. It's write-only, so it never reaches the state or a plan; destroy doesn't need it."
  type        = string
  sensitive   = true
  ephemeral   = true
  default     = null
}
