output "cluster_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.main.name
}

output "account_id" {
  description = "AWS account ID detected from the active credentials."
  value       = local.account_id
}

output "ecr_registry" {
  description = "ECR registry hostname."
  value       = local.ecr_registry
}

output "backend_repository_url" {
  description = "Backend ECR repository URL."
  value       = aws_ecr_repository.backend.repository_url
}

output "frontend_repository_url" {
  description = "Frontend ECR repository URL."
  value       = aws_ecr_repository.frontend.repository_url
}

output "github_actions_ecr_push_role_arn" {
  description = "GitHub Actions role ARN for pushing images to ECR."
  value       = aws_iam_role.ecr_push_hospitalsystem.arn
}

output "github_actions_deploy_role_arn" {
  description = "GitHub Actions role ARN for deploying to EKS."
  value       = aws_iam_role.eks_deploy_hospitalsystem.arn
}

output "load_balancer_controller_role_arn" {
  description = "AWS Load Balancer Controller IAM role ARN."
  value       = aws_iam_role.load_balancer_controller.arn
}

output "eks_node_role_arn" {
  description = "EKS node group IAM role ARN."
  value       = aws_iam_role.eks_node.arn
}

output "acm_certificate_arn" {
  description = "ACM certificate ARN used by the Kubernetes Ingress."
  value       = aws_acm_certificate.app.arn
}

output "acm_validation_record_name" {
  description = "ACM DNS validation record name."
  value       = local.acm_validation_record.name
}

output "acm_validation_record_type" {
  description = "ACM DNS validation record type."
  value       = local.acm_validation_record.type
}

output "acm_validation_record_value" {
  description = "ACM DNS validation record value."
  value       = local.acm_validation_record.value
}

output "vpc_id" {
  description = "VPC ID."
  value       = aws_vpc.kubes.id
}

output "public_subnet_ids" {
  description = "Public subnet IDs used by internet-facing load balancers."
  value       = [aws_subnet.public_a.id, aws_subnet.public_b.id]
}

output "private_subnet_ids" {
  description = "Private subnet IDs used by EKS workloads."
  value       = [aws_subnet.private_a.id, aws_subnet.private_b.id]
}
