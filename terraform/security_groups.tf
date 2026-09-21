resource "aws_security_group" "ops" {
  name        = local.ops_name
  description = "Ops instance for private EKS API access: HTTPS inside the VPC and to S3 only"
  vpc_id      = aws_vpc.kubes.id

  tags = {
    Name = "${local.ops_name}-Terraform"
  }
}

resource "aws_vpc_security_group_egress_rule" "ops_vpc_https" {
  security_group_id = aws_security_group.ops.id
  description       = "HTTPS to the SSM VPC endpoints and the private EKS endpoint"
  cidr_ipv4         = aws_vpc.kubes.cidr_block
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "ops_s3_https" {
  security_group_id = aws_security_group.ops.id
  description       = "HTTPS to S3 through the gateway endpoint, for package updates"
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "eks_api_from_ops" {
  security_group_id            = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
  description                  = "Kubernetes API from the ops instance"
  referenced_security_group_id = aws_security_group.ops.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_security_group" "vpc_endpoints" {
  name        = "${local.vpc_name}-vpc-endpoints"
  description = "Interface VPC endpoints: HTTPS from inside the VPC"
  vpc_id      = aws_vpc.kubes.id

  tags = {
    Name = "${local.vpc_name}-vpc-endpoints-Terraform"
  }
}

resource "aws_vpc_security_group_ingress_rule" "vpc_endpoints_https" {
  security_group_id = aws_security_group.vpc_endpoints.id
  description       = "HTTPS from the nodes, pods and ops instance"
  cidr_ipv4         = aws_vpc.kubes.cidr_block
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
