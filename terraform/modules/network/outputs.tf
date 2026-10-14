output "vpc_id" {
  description = "VPC ID."
  value       = aws_vpc.main.id
}

output "vpc_arn" {
  description = "VPC ARN."
  value       = aws_vpc.main.arn
}

output "vpc_cidr_block" {
  description = "CIDR block of the VPC."
  value       = aws_vpc.main.cidr_block
}

output "public_subnet_ids" {
  description = "Public subnet IDs, in availability_zones order."
  value       = [for zone in var.availability_zones : aws_subnet.public[zone].id]
}

output "public_subnet_cidrs" {
  description = "Public subnet CIDR blocks, in availability_zones order."
  value       = [for zone in var.availability_zones : aws_subnet.public[zone].cidr_block]
}

output "private_subnet_ids" {
  description = "Private subnet IDs, in availability_zones order. The first one holds the interface endpoints."
  value       = [for zone in var.availability_zones : aws_subnet.private[zone].id]
}

output "nat_public_ips" {
  description = "Public IPs of the NAT gateways, which the private subnets' outbound traffic comes from."
  value       = [for zone in local.nat_zones : aws_eip.nat[zone].public_ip]
}

output "s3_prefix_list_id" {
  description = "Prefix list of the S3 gateway endpoint, for security group rules."
  value       = aws_vpc_endpoint.s3.prefix_list_id
}

output "ecr_api_endpoint_dns_name" {
  description = "DNS name of the ECR API interface endpoint, which has no private DNS."
  value       = aws_vpc_endpoint.ecr_api.dns_entry[0].dns_name
}
