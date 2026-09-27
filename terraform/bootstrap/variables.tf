variable "bucket_name" {
  description = "The state bucket's name. scripts/tf.sh passes hospitalsystem-tfstate-<account ID>."
  type        = string
}

variable "aws_region" {
  description = "Region of the state bucket. Keep it the same as the main configuration's region and its backend block."
  type        = string
  default     = "eu-north-1"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "Project name, used in the tags."
  type        = string
  default     = "hospitalsystem"
}

variable "noncurrent_version_days" {
  description = "Days an overwritten state version is kept before S3 deletes it."
  type        = number
  default     = 90
}
