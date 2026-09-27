terraform {
  # use_lockfile needs 1.11.
  required_version = ">= 1.11.0"

  # State lives in the bucket terraform/bootstrap creates. Its name has the
  # account ID in it, so ./scripts/tf.sh passes it to terraform init.
  backend "s3" {
    key          = "hospitalsystem/terraform.tfstate"
    region       = "eu-north-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }

    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }

    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}
