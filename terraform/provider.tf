provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.default_tags
  }
}


provider "cloudflare" {
  api_token = var.cloudflare_api_token != "" ? var.cloudflare_api_token : null
}
