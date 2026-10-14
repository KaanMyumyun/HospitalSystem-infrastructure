variable "cluster_name" {
  description = "EKS cluster name, also used in the names of its log group and IAM roles."
  type        = string
}

variable "kubernetes_version" {
  description = "Kubernetes version of the control plane. The add-ons follow EKS's default version for it."
  type        = string
}

variable "environment" {
  description = "Deployment environment name, tagged on the cluster."
  type        = string
}

variable "aws_region" {
  description = "AWS region of the cluster."
  type        = string
}

variable "account_id" {
  description = "AWS account ID, which the Load Balancer Controller's permissions are limited to."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets for the control plane's network interfaces and the nodes."
  type        = list(string)
}

variable "vpc_arn" {
  description = "VPC the Load Balancer Controller may create security groups in."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key that encrypts Kubernetes Secrets and the control plane log group."
  type        = string
}

variable "certificate_arn" {
  description = "ACM certificate the Load Balancer Controller may read for the Ingress's HTTPS listener."
  type        = string
}

variable "node_group_name" {
  description = "Name of the managed node group."
  type        = string
}

variable "node_role_name" {
  description = "Name of the nodes' IAM role."
  type        = string
}

variable "node_instance_types" {
  description = "EC2 instance types of the node group."
  type        = list(string)
}

variable "node_desired_size" {
  description = "Node count the node group starts with. Later changes to the desired count, such as pause.yml's, are left alone."
  type        = number
}

variable "node_max_size" {
  description = "Most nodes the node group can have."
  type        = number
}
