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

  # Node count when the app is running. ansible/playbooks/resume.yml scales
  # back to it after pause.yml scaled the node group to 0.
  node_desired_size = 2

  k8s_namespace    = "hospitalsystem"
  k8s_deploy_group = "${local.k8s_namespace}:deployers"
  ops_name         = "${var.project_name}-ops"
  ecr_registry     = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"

  github_oidc_provider_arn = var.github_oidc_provider_arn != "" ? var.github_oidc_provider_arn : aws_iam_openid_connect_provider.github_actions[0].arn

  ec2_arn_prefix  = "arn:aws:ec2:${var.aws_region}:${local.account_id}"
  elb_arn_prefix  = "arn:aws:elasticloadbalancing:${var.aws_region}:${local.account_id}"
  elb_arns        = ["${local.elb_arn_prefix}:loadbalancer/app/*/*", "${local.elb_arn_prefix}:loadbalancer/net/*/*"]
  listener_arns   = ["${local.elb_arn_prefix}:listener/app/*/*/*", "${local.elb_arn_prefix}:listener/net/*/*/*"]
  rule_arns       = ["${local.elb_arn_prefix}:listener-rule/app/*/*/*/*", "${local.elb_arn_prefix}:listener-rule/net/*/*/*/*"]
  target_grp_arns = ["${local.elb_arn_prefix}:targetgroup/*/*"]

  backend_source_dir  = var.backend_source_dir != "" ? var.backend_source_dir : abspath("${path.module}/../../HospitalSystem")
  frontend_source_dir = var.frontend_source_dir != "" ? var.frontend_source_dir : "${local.backend_source_dir}/hospital-frontend"

  ansible_vars = {
    aws_region                        = var.aws_region
    eks_cluster_name                  = local.cluster_name
    eks_node_group_name               = aws_eks_node_group.hospitalsystempr1.node_group_name
    eks_node_desired_size             = local.node_desired_size
    vpc_id                            = aws_vpc.kubes.id
    public_subnet_cidrs               = [aws_subnet.public_a.cidr_block, aws_subnet.public_b.cidr_block]
    backend_repository_url            = aws_ecr_repository.backend.repository_url
    frontend_repository_url           = aws_ecr_repository.frontend.repository_url
    initial_image_tag                 = var.initial_image_tag
    load_balancer_controller_role_arn = aws_iam_role.load_balancer_controller.arn
    backend_secret_name               = aws_secretsmanager_secret.backend.name
    backend_secrets_reader_role_arn   = aws_iam_role.backend_secrets_reader.arn
    acm_certificate_arn               = aws_acm_certificate.app.arn
    app_domain_name                   = var.app_domain_name
    cloudflare_zone_name              = var.cloudflare_zone_name
    monitoring_alarm_prefix           = var.project_name
    monitoring_alert_sns_topic_arn    = aws_sns_topic.alerts.arn
    k8s_namespace                     = local.k8s_namespace
    github_actions_deploy_group       = local.k8s_deploy_group
    ops_instance_id                   = aws_instance.ops.id
    deploy_ssm_document_name          = aws_ssm_document.deploy.name
  }

  # Track bootstrap inputs only. Deploy, scale, cleanup, and status playbooks
  # are independent operations and must not cause Terraform to rerun bootstrap.
  # Keep this list in sync with the imports in bootstrap.yml.
  ansible_bootstrap_files = sort(concat(
    [
      "ansible.cfg",
      "ansible/inventory.ini",
      "ansible/playbooks/bootstrap.yml",
      "ansible/playbooks/kubeconfig.yml",
      "ansible/playbooks/external-secrets.yml",
      "ansible/playbooks/backend-secret.yml",
      "ansible/playbooks/load-balancer-controller.yml",
      "ansible/playbooks/metrics-server.yml",
      "ansible/playbooks/apply-kubernetes.yml",
      "ansible/playbooks/cloudflare-dns.yml",
      "ansible/playbooks/monitoring.yml",
      "scripts/apply-workload.py",
    ],
    [
      for file in fileset("${path.module}/../ansible", "group_vars/**/*.yml") :
      "ansible/${file}"
      if file != "group_vars/all/terraform.yml"
    ],
    [
      for file in fileset("${path.module}/../ansible", "tasks/**/*.yml") :
      "ansible/${file}"
    ],
    [
      for file in fileset("${path.module}/../kubernetes", "**/*.j2") :
      "kubernetes/${file}"
    ]
  ))
  # Generated Terraform variables are tracked separately by bootstrap_vars_hash.
  ansible_bootstrap_file_hashes = {
    for file in local.ansible_bootstrap_files :
    file => filesha256("${path.module}/../${file}")
  }
  ansible_bootstrap_files_hash = sha256(jsonencode(local.ansible_bootstrap_file_hashes))
}
