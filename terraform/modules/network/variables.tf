variable "name" {
  description = "Name of the VPC, used in the names of its log group, flow log role, endpoints and security group."
  type        = string
}

variable "cidr_block" {
  description = "CIDR block of the VPC. Each availability zone gets a public and a private subnet, a sixteenth of it each."
  type        = string
}

variable "aws_region" {
  description = "AWS region of the VPC."
  type        = string
}

variable "availability_zones" {
  description = "Letters of the availability zones in aws_region the VPC spans, for example [\"a\", \"b\"]."
  type        = list(string)
}

variable "single_nat_gateway" {
  description = "Route every private subnet through one NAT gateway, in the first availability zone, instead of one per zone."
  type        = bool
}

variable "cluster_name" {
  description = "EKS cluster whose load balancers may use the subnets; they're tagged for it."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key that encrypts the flow log group."
  type        = string
}

variable "account_id" {
  description = "AWS account ID, which the flow log role's trust policy is limited to."
  type        = string
}
