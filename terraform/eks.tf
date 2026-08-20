resource "aws_eks_cluster" "main" {
  name     = local.cluster_name
  role_arn = aws_iam_role.eks_cluster.arn
  version  = "1.36"

  kubernetes_network_config {
    ip_family         = "ipv4"
    service_ipv4_cidr = "172.20.0.0/16"
  }

  vpc_config {
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = ["0.0.0.0/0"]
    subnet_ids              = [aws_subnet.private_a.id, aws_subnet.private_b.id]
  }

  tags = {
    ENV = var.environment
    pr1 = "kubenetes"
  }

  depends_on = [aws_iam_role_policy_attachment.eks_cluster_policy]
}

resource "aws_eks_node_group" "hospitalsystempr1" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "hospitalsystempr1"
  node_role_arn   = aws_iam_role.eks_node.arn
  subnet_ids      = [aws_subnet.private_a.id, aws_subnet.private_b.id]

  ami_type       = "AL2023_x86_64_STANDARD"
  capacity_type  = "ON_DEMAND"
  disk_size      = 20
  instance_types = ["t3.micro"]

  scaling_config {
    desired_size = 0
    max_size     = 1
    min_size     = 0
  }

  update_config {
    max_unavailable = 1
  }

  labels = {
    "eks.amazonaws.com/capacityType" = "ON_DEMAND"
    "eks.amazonaws.com/nodegroup"    = "hospitalsystempr1"
  }

  tags = {
    "eks:cluster-name"                                = local.cluster_name
    "eks:nodegroup-name"                              = "hospitalsystempr1"
    "k8s.io/cluster-autoscaler/enabled"               = "true"
    "k8s.io/cluster-autoscaler/${local.cluster_name}" = "owned"
    (local.eks_cluster_tag_key)                       = "owned"
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
