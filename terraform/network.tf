resource "aws_vpc" "kubes" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  instance_tenancy     = "default"

  tags = {
    Name = "${local.vpc_name}-Terraform"
  }
}

resource "aws_default_security_group" "kubes" {
  vpc_id = aws_vpc.kubes.id

  tags = {
    Name = "${local.vpc_name}-default-Terraform"
  }
}

resource "aws_cloudwatch_log_group" "vpc_flow_logs" {
  name              = "/aws/vpc/${local.vpc_name}/flow-logs"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_flow_log" "kubes" {
  vpc_id               = aws_vpc.kubes.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.vpc_flow_logs.arn
  iam_role_arn         = aws_iam_role.vpc_flow_logs.arn

  tags = {
    Name = "${local.vpc_name}-flow-logs-Terraform"
  }
}

resource "aws_vpc_endpoint" "ssm" {
  for_each = toset(["ssm", "ssmmessages", "ec2messages"])

  vpc_id              = aws_vpc.kubes.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [aws_subnet.private_a.id]
  security_group_ids  = [aws_security_group.vpc_endpoints.id]

  tags = {
    Name = "${local.vpc_name}-${each.key}-Terraform"
  }
}

# For the deploy script's image check on the ops instance, which calls it by
# its own DNS name. No private DNS, so the nodes keep reaching ECR through NAT
# and don't come to depend on an endpoint in one subnet.
resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = aws_vpc.kubes.id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = false
  subnet_ids          = [aws_subnet.private_a.id]
  security_group_ids  = [aws_security_group.vpc_endpoints.id]

  tags = {
    Name = "${local.vpc_name}-ecr-api-Terraform"
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.kubes.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private_a.id, aws_route_table.private_b.id]

  tags = {
    Name = "${local.vpc_name}-s3-Terraform"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.kubes.id

  tags = {
    Name = "${local.vpc_name}-igw-Terraform"
  }
}

resource "aws_subnet" "public_a" {
  vpc_id                              = aws_vpc.kubes.id
  cidr_block                          = "10.0.0.0/20"
  availability_zone                   = "${var.aws_region}a"
  private_dns_hostname_type_on_launch = "ip-name"

  tags = {
    Name                        = "p1-Terraform"
    az                          = "1"
    (local.eks_cluster_tag_key) = "shared"
    "kubernetes.io/role/elb"    = "1"
  }
}

resource "aws_subnet" "public_b" {
  vpc_id                              = aws_vpc.kubes.id
  cidr_block                          = "10.0.16.0/20"
  availability_zone                   = "${var.aws_region}b"
  private_dns_hostname_type_on_launch = "ip-name"

  tags = {
    Name                        = "p2-Terraform"
    az                          = "2"
    (local.eks_cluster_tag_key) = "shared"
    "kubernetes.io/role/elb"    = "1"
  }
}

resource "aws_subnet" "private_a" {
  vpc_id                              = aws_vpc.kubes.id
  cidr_block                          = "10.0.32.0/20"
  availability_zone                   = "${var.aws_region}a"
  private_dns_hostname_type_on_launch = "ip-name"

  tags = {
    Name                              = "private1-Terraform"
    az                                = "1"
    (local.eks_cluster_tag_key)       = "shared"
    "kubernetes.io/role/internal-elb" = "1"
  }
}

resource "aws_subnet" "private_b" {
  vpc_id                              = aws_vpc.kubes.id
  cidr_block                          = "10.0.48.0/20"
  availability_zone                   = "${var.aws_region}b"
  private_dns_hostname_type_on_launch = "ip-name"

  tags = {
    Name                              = "private2-Terraform"
    az                                = "2"
    (local.eks_cluster_tag_key)       = "shared"
    "kubernetes.io/role/internal-elb" = "1"
  }
}

resource "aws_eip" "nat_a" {
  domain               = "vpc"
  network_border_group = var.aws_region
  public_ipv4_pool     = "amazon"

  tags = {
    Name = "nat1-Terraform"
  }
}

resource "aws_eip" "nat_b" {
  domain               = "vpc"
  network_border_group = var.aws_region
  public_ipv4_pool     = "amazon"

  tags = {
    Name = "nat2-Terraform"
  }
}

resource "aws_nat_gateway" "nat_a" {
  allocation_id     = aws_eip.nat_a.id
  connectivity_type = "public"
  subnet_id         = aws_subnet.public_a.id

  tags = {
    Name = "nat1-Terraform"
  }
}

resource "aws_nat_gateway" "nat_b" {
  allocation_id     = aws_eip.nat_b.id
  connectivity_type = "public"
  subnet_id         = aws_subnet.public_b.id

  tags = {
    Name = "nat2-Terraform"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.kubes.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "public-Terraform"
  }
}

resource "aws_route_table" "private_a" {
  vpc_id = aws_vpc.kubes.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat_a.id
  }

  tags = {
    Name = "rt-private-1-Terraform"
  }
}

resource "aws_route_table" "private_b" {
  vpc_id = aws_vpc.kubes.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat_b.id
  }

  tags = {
    Name = "rt-private2-Terraform"
  }
}

resource "aws_route_table_association" "public_a" {
  subnet_id      = aws_subnet.public_a.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "public_b" {
  subnet_id      = aws_subnet.public_b.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private_a" {
  subnet_id      = aws_subnet.private_a.id
  route_table_id = aws_route_table.private_a.id
}

resource "aws_route_table_association" "private_b" {
  subnet_id      = aws_subnet.private_b.id
  route_table_id = aws_route_table.private_b.id
}
