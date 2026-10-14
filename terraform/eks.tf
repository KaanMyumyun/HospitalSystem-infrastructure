# No depends_on here: the KMS key policy names the cluster role, and the
# cluster uses the key. Terraform orders the resources inside the module one
# by one, which a depends_on on the whole module would turn into a cycle.
module "eks" {
  source = "./modules/eks"

  cluster_name       = local.cluster_name
  kubernetes_version = var.eks_version
  environment        = var.environment
  aws_region         = var.aws_region
  account_id         = local.account_id
  subnet_ids         = module.network.private_subnet_ids
  vpc_arn            = module.network.vpc_arn
  kms_key_arn        = aws_kms_key.main.arn
  certificate_arn    = aws_acm_certificate.app.arn

  node_group_name     = "hospitalsystempr1"
  node_role_name      = "hospital-pr1-node-role"
  node_instance_types = var.node_instance_types
  node_desired_size   = var.node_desired_size
  node_max_size       = var.node_max_size
}
