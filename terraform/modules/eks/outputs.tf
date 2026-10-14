output "cluster_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.main.name
}

output "endpoint" {
  description = "URL of the cluster's private Kubernetes API endpoint."
  value       = aws_eks_cluster.main.endpoint
}

output "certificate_authority_data" {
  description = "Base64 CA certificate of the Kubernetes API."
  value       = aws_eks_cluster.main.certificate_authority[0].data
}

output "cluster_security_group_id" {
  description = "Security group EKS creates for the cluster. The control plane, the nodes and the pods use it."
  value       = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
}

output "cluster_role_arn" {
  description = "IAM role of the control plane, which needs the KMS key for Secrets encryption."
  value       = aws_iam_role.eks_cluster.arn
}

output "oidc_provider_arn" {
  description = "IAM OIDC provider of the cluster, for service account roles."
  value       = aws_iam_openid_connect_provider.eks.arn
}

output "oidc_provider_host" {
  description = "Issuer of the cluster's OIDC provider without https://, the prefix of the :aud and :sub keys in trust policies."
  value       = replace(aws_iam_openid_connect_provider.eks.url, "https://", "")
}

output "node_group_name" {
  description = "Name of the managed node group."
  value       = aws_eks_node_group.main.node_group_name
}

output "node_asg_name" {
  description = "Auto Scaling group EKS created for the node group."
  value       = aws_eks_node_group.main.resources[0].autoscaling_groups[0].name
}

output "node_role_arn" {
  description = "IAM role of the nodes."
  value       = aws_iam_role.eks_node.arn
}

output "load_balancer_controller_role_arn" {
  description = "IAM role of the AWS Load Balancer Controller's service account."
  value       = aws_iam_role.load_balancer_controller.arn
}
