locals {
  account_id   = "147914447694"
  cluster_name = "eks-pr1"
  vpc_name     = "kubes"

  default_tags = {
    Environment = var.environment
    Project     = var.project_name
    ManagedBy   = "terraform"
  }

  eks_cluster_tag_key = "kubernetes.io/cluster/${local.cluster_name}"

  oidc_provider_arn = "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
}
