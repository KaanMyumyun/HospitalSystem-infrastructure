locals {
  cluster_tag_key = "kubernetes.io/cluster/${var.cluster_name}"
}

resource "aws_cloudwatch_log_group" "eks_cluster" {
  name              = "/aws/eks/${var.cluster_name}/cluster"
  retention_in_days = 365
  kms_key_id        = var.kms_key_arn
}

resource "aws_eks_cluster" "main" {
  name     = var.cluster_name
  role_arn = aws_iam_role.eks_cluster.arn
  version  = var.kubernetes_version

  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  encryption_config {
    resources = ["secrets"]

    provider {
      key_arn = var.kms_key_arn
    }
  }

  kubernetes_network_config {
    ip_family         = "ipv4"
    service_ipv4_cidr = "172.20.0.0/16"
  }

  zonal_shift_config {
    enabled = false
  }

  access_config {
    # Preserve existing managed-node and other aws-auth mappings during migration.
    # Deploy roles use access entries, which take precedence for those principals.
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = true
  }

  vpc_config {
    endpoint_private_access = true
    endpoint_public_access  = false
    subnet_ids              = var.subnet_ids
  }

  tags = {
    Name = "${var.cluster_name}-Terraform"
    ENV  = var.environment
  }

  depends_on = [
    aws_cloudwatch_log_group.eks_cluster,
    aws_iam_role_policy_attachment.eks_cluster_policy
  ]
}

resource "aws_eks_node_group" "main" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = var.node_group_name
  node_role_arn   = aws_iam_role.eks_node.arn
  subnet_ids      = var.subnet_ids

  ami_type       = "AL2023_x86_64_STANDARD"
  capacity_type  = "ON_DEMAND"
  disk_size      = 20
  instance_types = var.node_instance_types

  # min_size stays 0 so ansible/playbooks/pause.yml can scale the nodes away.
  scaling_config {
    desired_size = var.node_desired_size
    max_size     = var.node_max_size
    min_size     = 0
  }

  update_config {
    max_unavailable = 1
    update_strategy = "DEFAULT"
  }

  tags = {
    Name                    = "${var.node_group_name}-Terraform"
    "eks:cluster-name"      = var.cluster_name
    "eks:nodegroup-name"    = var.node_group_name
    (local.cluster_tag_key) = "owned"
  }

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_node_cni,
    aws_iam_role_policy_attachment.eks_node_ecr_public_readonly,
    aws_iam_role_policy_attachment.eks_node_ecr_readonly,
    aws_iam_role_policy_attachment.eks_node_worker
  ]
}

data "aws_eks_addon_version" "core" {
  for_each = toset(["vpc-cni", "coredns", "kube-proxy"])

  addon_name         = each.key
  kubernetes_version = aws_eks_cluster.main.version
}

resource "aws_eks_addon" "core" {
  for_each = data.aws_eks_addon_version.core

  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = each.key
  addon_version               = each.value.version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  preserve                    = true

  # Without this the VPC CNI accepts NetworkPolicies but enforces none of them.
  configuration_values = each.key == "vpc-cni" ? jsonencode({ enableNetworkPolicy = "true" }) : null

  depends_on = [aws_eks_node_group.main]
}
