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
    Name             = "http-https-Terraform"
    "http and https" = "80 and 443"
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

  tags = {
    Name = "postgress-Terraform"
  }
}
