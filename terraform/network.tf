resource "aws_vpc" "kubes" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = false
  instance_tenancy     = "default"

  tags = {
    Name = local.vpc_name
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.kubes.id

  tags = {
    Name = "hello"
  }
}

resource "aws_internet_gateway" "detached" {
  tags = {
    Name = "ig"
  }
}

resource "aws_subnet" "public_a" {
  vpc_id                              = aws_vpc.kubes.id
  cidr_block                          = "10.0.0.0/20"
  availability_zone                   = "${var.aws_region}a"
  private_dns_hostname_type_on_launch = "ip-name"

  tags = {
    Name                        = "p1"
    Purpose                     = "AZ1"
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
    Name                        = "p2"
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
    Name                              = "private1"
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
    Name                              = "private2"
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
    Name = "nat1"
  }
}

resource "aws_eip" "nat_b" {
  domain               = "vpc"
  network_border_group = var.aws_region
  public_ipv4_pool     = "amazon"

  tags = {
    Name = "nat2"
  }
}

resource "aws_eip" "external_1" {
  domain               = "vpc"
  network_border_group = var.aws_region
  public_ipv4_pool     = "amazon"

  lifecycle {
    ignore_changes = [network_interface]
  }
}

resource "aws_eip" "external_2" {
  domain               = "vpc"
  network_border_group = var.aws_region
  public_ipv4_pool     = "amazon"

  lifecycle {
    ignore_changes = [network_interface]
  }
}

resource "aws_nat_gateway" "nat_a" {
  allocation_id     = aws_eip.nat_a.id
  connectivity_type = "public"
  subnet_id         = aws_subnet.public_a.id

  tags = {
    Name = "nat1"
  }
}

resource "aws_nat_gateway" "nat_b" {
  allocation_id     = aws_eip.nat_b.id
  connectivity_type = "public"
  subnet_id         = aws_subnet.public_b.id

  tags = {
    Name = "nat2"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.kubes.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "public"
  }
}

resource "aws_route_table" "private_a" {
  vpc_id = aws_vpc.kubes.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat_a.id
  }

  tags = {
    Name = "rt-private-1"
  }
}

resource "aws_route_table" "private_b" {
  vpc_id = aws_vpc.kubes.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat_b.id
  }

  tags = {
    Name = "rt-private2"
  }
}

resource "aws_route_table" "default" {
  vpc_id = aws_vpc.kubes.id
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
