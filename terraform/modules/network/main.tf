locals {
  # Public subnets take the first sixteenths of the VPC and private ones the
  # next, so two zones in 10.0.0.0/16 get 10.0.0.0/20 and 10.0.16.0/20
  # public, 10.0.32.0/20 and 10.0.48.0/20 private. Zones are numbered from 1
  # for the names (p1, private1, nat1).
  zones = {
    for index, letter in var.availability_zones : letter => {
      name         = "${var.aws_region}${letter}"
      number       = index + 1
      public_cidr  = cidrsubnet(var.cidr_block, 4, index)
      private_cidr = cidrsubnet(var.cidr_block, 4, index + length(var.availability_zones))
    }
  }
  first_zone = var.availability_zones[0]
  nat_zones  = var.single_nat_gateway ? [local.first_zone] : var.availability_zones

  cluster_tag_key = "kubernetes.io/cluster/${var.cluster_name}"
}

resource "aws_vpc" "main" {
  cidr_block           = var.cidr_block
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.name}-Terraform"
  }
}

resource "aws_default_security_group" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.name}-default-Terraform"
  }
}

resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/aws/vpc/${var.name}/flow-logs"
  retention_in_days = 365
  kms_key_id        = var.kms_key_arn
}

data "aws_iam_policy_document" "flow_logs_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  name                 = "${var.name}-vpc-flow-logs"
  path                 = "/"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.flow_logs_assume_role.json
}

resource "aws_iam_role_policy" "flow_logs" {
  name = "${var.name}-vpc-flow-logs-write"
  role = aws_iam_role.flow_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WriteFlowLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:DescribeLogStreams",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.flow_logs.arn}:*"
      }
    ]
  })
}

resource "aws_flow_log" "main" {
  vpc_id               = aws_vpc.main.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow_logs.arn
  iam_role_arn         = aws_iam_role.flow_logs.arn

  tags = {
    Name = "${var.name}-flow-logs-Terraform"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.name}-igw-Terraform"
  }
}

resource "aws_subnet" "public" {
  for_each = local.zones

  vpc_id            = aws_vpc.main.id
  cidr_block        = each.value.public_cidr
  availability_zone = each.value.name

  tags = {
    Name                     = "p${each.value.number}-Terraform"
    (local.cluster_tag_key)  = "shared"
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_subnet" "private" {
  for_each = local.zones

  vpc_id            = aws_vpc.main.id
  cidr_block        = each.value.private_cidr
  availability_zone = each.value.name

  tags = {
    Name                              = "private${each.value.number}-Terraform"
    (local.cluster_tag_key)           = "shared"
    "kubernetes.io/role/internal-elb" = "1"
  }
}

resource "aws_eip" "nat" {
  for_each = toset(local.nat_zones)

  domain = "vpc"

  tags = {
    Name = "nat${local.zones[each.key].number}-Terraform"
  }
}

resource "aws_nat_gateway" "main" {
  for_each = toset(local.nat_zones)

  allocation_id = aws_eip.nat[each.key].id
  subnet_id     = aws_subnet.public[each.key].id

  tags = {
    Name = "nat${local.zones[each.key].number}-Terraform"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "rt-public-Terraform"
  }
}

resource "aws_route_table" "private" {
  for_each = local.zones

  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[var.single_nat_gateway ? local.first_zone : each.key].id
  }

  tags = {
    Name = "rt-private${each.value.number}-Terraform"
  }
}

resource "aws_route_table_association" "public" {
  for_each = local.zones

  subnet_id      = aws_subnet.public[each.key].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  for_each = local.zones

  subnet_id      = aws_subnet.private[each.key].id
  route_table_id = aws_route_table.private[each.key].id
}

resource "aws_security_group" "vpc_endpoints" {
  name        = "${var.name}-vpc-endpoints"
  description = "Interface VPC endpoints: HTTPS from inside the VPC"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${var.name}-vpc-endpoints-Terraform"
  }
}

resource "aws_vpc_security_group_ingress_rule" "vpc_endpoints_https" {
  security_group_id = aws_security_group.vpc_endpoints.id
  description       = "HTTPS from the nodes, pods and ops instance"
  cidr_ipv4         = aws_vpc.main.cidr_block
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_endpoint" "ssm" {
  for_each = toset(["ssm", "ssmmessages", "ec2messages"])

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [aws_subnet.private[local.first_zone].id]
  security_group_ids  = [aws_security_group.vpc_endpoints.id]

  tags = {
    Name = "${var.name}-${each.key}-Terraform"
  }
}

# For the deploy script's image check on the ops instance, which calls it by
# its own DNS name. No private DNS, so the nodes keep reaching ECR through NAT
# and don't come to depend on an endpoint in one subnet.
resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = false
  subnet_ids          = [aws_subnet.private[local.first_zone].id]
  security_group_ids  = [aws_security_group.vpc_endpoints.id]

  tags = {
    Name = "${var.name}-ecr-api-Terraform"
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [for zone in var.availability_zones : aws_route_table.private[zone].id]

  tags = {
    Name = "${var.name}-s3-Terraform"
  }
}
