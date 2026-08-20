resource "aws_security_group" "default" {
  name        = "default"
  description = "default VPC security group"
  vpc_id      = aws_vpc.kubes.id

  ingress {
    from_port = 0
    protocol  = "-1"
    self      = true
    to_port   = 0
  }

  egress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 0
    protocol    = "-1"
    to_port     = 0
  }

  tags = {
    Name = "default"
  }
}

resource "aws_security_group" "eks_cluster" {
  name        = "eks-cluster-sg-eks-pr1-1680432849"
  description = "EKS created security group applied to EKS control plane ENIs and managed workloads."
  vpc_id      = aws_vpc.kubes.id

  ingress {
    description = "Allows EFA traffic, which is not matched by CIDR rules."
    from_port   = 0
    protocol    = "-1"
    self        = true
    to_port     = 0
  }

  egress {
    description = "Allows EFA traffic, which is not matched by CIDR rules."
    from_port   = 0
    protocol    = "-1"
    self        = true
    to_port     = 0
  }

  egress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 0
    protocol    = "-1"
    to_port     = 0
  }

  tags = {
    Name                        = "eks-cluster-sg-eks-pr1-1680432849"
    (local.eks_cluster_tag_key) = "owned"
  }
}

resource "aws_security_group" "http_https" {
  name        = "http-https"
  description = "http and https"
  vpc_id      = aws_vpc.kubes.id

  ingress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 80
    protocol    = "tcp"
    to_port     = 80
  }

  ingress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 443
    protocol    = "tcp"
    to_port     = 443
  }

  egress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 0
    protocol    = "-1"
    to_port     = 0
  }

  tags = {
    Name             = "http-https"
    "http and https" = "80 and 443"
  }
}

resource "aws_security_group" "alb_managed" {
  name        = "k8s-hospital-hospital-e639bad66b"
  description = "[k8s] Managed SecurityGroup for LoadBalancer"
  vpc_id      = aws_vpc.kubes.id

  ingress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 80
    protocol    = "tcp"
    to_port     = 80
  }

  ingress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 443
    protocol    = "tcp"
    to_port     = 443
  }

  egress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 0
    protocol    = "-1"
    to_port     = 0
  }

  tags = {
    "elbv2.k8s.aws/cluster"    = local.cluster_name
    "ingress.k8s.aws/resource" = "ManagedLBSecurityGroup"
    "ingress.k8s.aws/stack"    = "hospitalsystem/hospital-ingress"
  }

  lifecycle {
    ignore_changes = [ingress, egress, tags]
  }
}

resource "aws_security_group" "alb_backend" {
  name        = "k8s-traffic-ekspr1-6115ae2f89"
  description = "[k8s] Shared Backend SecurityGroup for LoadBalancer"
  vpc_id      = aws_vpc.kubes.id

  egress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 0
    protocol    = "-1"
    to_port     = 0
  }

  tags = {
    "elbv2.k8s.aws/cluster"  = local.cluster_name
    "elbv2.k8s.aws/resource" = "backend-sg"
  }

  lifecycle {
    ignore_changes = [ingress, egress, tags]
  }
}

resource "aws_security_group" "postgres" {
  name        = "postgress"
  description = "Security group for PostgreSQL RDS"
  vpc_id      = aws_vpc.kubes.id

  ingress {
    from_port        = 5432
    ipv6_cidr_blocks = ["::/0"]
    protocol         = "tcp"
    to_port          = 5432
  }

  egress {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 0
    protocol    = "-1"
    to_port     = 0
  }
}
