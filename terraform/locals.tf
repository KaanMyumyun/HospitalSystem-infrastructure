locals {
  account_id   = data.aws_caller_identity.current.account_id
  cluster_name = "eks-pr1"
  vpc_name     = "kubes"

  default_tags = {
    Environment = var.environment
    Project     = var.project_name
    ManagedBy   = "terraform"
  }

  eks_cluster_tag_key = "kubernetes.io/cluster/${local.cluster_name}"
  ecr_registry        = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"

  frontend_api_url = var.frontend_api_url != "" ? var.frontend_api_url : "https://${var.app_domain_name}/api"

  backend_source_dir  = var.backend_source_dir != "" ? var.backend_source_dir : abspath("${path.module}/../../HospitalSystem")
  frontend_source_dir = var.frontend_source_dir != "" ? var.frontend_source_dir : "${local.backend_source_dir}/hospital-frontend"

  ansible_bootstrap_vars = {
    aws_region                        = var.aws_region
    eks_cluster_name                  = local.cluster_name
    ecr_registry                      = local.ecr_registry
    backend_repository_url            = aws_ecr_repository.backend.repository_url
    frontend_repository_url           = aws_ecr_repository.frontend.repository_url
    backend_image                     = "${aws_ecr_repository.backend.repository_url}:${var.initial_image_tag}"
    frontend_image                    = "${aws_ecr_repository.frontend.repository_url}:${var.initial_image_tag}"
    github_actions_deploy_role_arn    = aws_iam_role.eks_deploy_hospitalsystem.arn
    eks_node_role_arn                 = aws_iam_role.eks_node.arn
    load_balancer_controller_role_arn = aws_iam_role.load_balancer_controller.arn
    acm_certificate_arn               = aws_acm_certificate.app.arn
    app_domain_name                   = var.app_domain_name
    cloudflare_zone_name              = var.cloudflare_zone_name
    cloudflare_record_name            = var.app_domain_name
  }

  ansible_bootstrap_file_hashes = concat(
    [
      for file in sort(fileset("${path.module}/../ansible/playbooks", "*.yml")) :
      filesha256("${path.module}/../ansible/playbooks/${file}")
    ],
    [filesha256("${path.module}/../ansible/group_vars/all.yml")],
    [
      for file in sort(fileset("${path.module}/../kubernetes", "**/*.yaml")) :
      filesha256("${path.module}/../kubernetes/${file}")
    ],
    [
      for file in sort(fileset("${path.module}/../kubernetes", "**/*.yaml.j2")) :
      filesha256("${path.module}/../kubernetes/${file}")
    ]
  )
  ansible_bootstrap_files_hash = sha256(join("", local.ansible_bootstrap_file_hashes))
}
