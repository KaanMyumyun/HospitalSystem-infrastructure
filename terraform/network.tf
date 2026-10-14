module "network" {
  source = "./modules/network"

  name               = local.vpc_name
  cidr_block         = "10.0.0.0/16"
  aws_region         = var.aws_region
  availability_zones = var.availability_zones
  single_nat_gateway = var.single_nat_gateway
  cluster_name       = local.cluster_name
  kms_key_arn        = aws_kms_key.main.arn
  account_id         = local.account_id
}
