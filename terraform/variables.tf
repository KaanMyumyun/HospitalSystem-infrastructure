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

variable "github_repository" {
  description = "GitHub repository allowed to assume deployment roles through OIDC."
  type        = string
  default     = "KaanMyumyun/HospitalSystem"
}

variable "acm_certificate_arn" {
  description = "ACM certificate used by the HTTPS ALB listener."
  type        = string
  default     = "arn:aws:acm:eu-north-1:147914447694:certificate/f82d2036-d650-45a0-bd19-3e67ccc16e39"
}
