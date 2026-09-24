output "app_url" {
  description = "Public HTTPS URL of the application."
  value       = "https://${var.app_domain_name}"
}

output "api_url" {
  description = "Backend API base URL behind the ALB. The frontend calls it as /api on its own host."
  value       = "https://${var.app_domain_name}/api"
}

output "github_actions_variables" {
  description = "Repository variables the workflows in KaanMyumyun/HospitalSystem read. Set these after every rebuild."
  value = {
    AWS_REGION                = var.aws_region
    AWS_ROLE_TO_ASSUME        = aws_iam_role.ecr_push_hospitalsystem.arn
    AWS_DEPLOY_ROLE_TO_ASSUME = aws_iam_role.eks_deploy_hospitalsystem.arn
    ECR_REGISTRY              = local.ecr_registry
    ECR_BACKEND_REPOSITORY    = aws_ecr_repository.backend.name
    ECR_FRONTEND_REPOSITORY   = aws_ecr_repository.frontend.name
    DEPLOY_SSM_DOCUMENT       = aws_ssm_document.deploy.name
    DEPLOY_INSTANCE_NAME      = local.ops_name
  }
}

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
  description = "GitHub Actions role ARN for deploying to EKS. Set it as the AWS_DEPLOY_ROLE_TO_ASSUME repository variable."
  value       = aws_iam_role.eks_deploy_hospitalsystem.arn
}

output "deploy_ssm_document_name" {
  description = "SSM document the deploy workflow sends to the ops instance. Set it as the DEPLOY_SSM_DOCUMENT repository variable."
  value       = aws_ssm_document.deploy.name
}

output "ops_instance_name" {
  description = "Name tag of the ops instance. Set it as the DEPLOY_INSTANCE_NAME repository variable."
  value       = local.ops_name
}

output "alerts_topic_arn" {
  description = "SNS topic every CloudWatch alarm notifies. Confirm the email subscription AWS sends after an apply."
  value       = aws_sns_topic.alerts.arn
}

output "ops_instance_id" {
  description = "Ops instance ID, the SSM target for reaching the private EKS API."
  value       = aws_instance.ops.id
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

output "nat_public_ips" {
  description = "Public IPs the cluster's outbound traffic comes from. Allow only these (plus your own) in Neon's IP Allow list."
  value       = [aws_eip.nat_a.public_ip, aws_eip.nat_b.public_ip]
}
