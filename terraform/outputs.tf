output "cluster_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.main.name
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
