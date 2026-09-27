# The S3 bucket that holds the main configuration's state. scripts/tf.sh
# applies this when the bucket is missing, and scripts/teardown.sh destroys it
# once that state is empty. It keeps its own state locally
# (terraform/bootstrap/terraform.tfstate, ignored by git): the bucket can't
# hold the state of the config that creates it.
terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Environment = var.environment
      Project     = var.project_name
      ManagedBy   = "terraform-bootstrap"
    }
  }
}
