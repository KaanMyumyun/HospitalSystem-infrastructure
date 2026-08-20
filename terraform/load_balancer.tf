resource "aws_lb" "hospital_system" {
  name                             = "hospital-system-alb"
  load_balancer_type               = "application"
  internal                         = false
  ip_address_type                  = "ipv4"
  security_groups                  = [aws_security_group.alb_backend.id, aws_security_group.alb_managed.id]
  subnets                          = [aws_subnet.public_a.id, aws_subnet.public_b.id]
  desync_mitigation_mode           = "defensive"
  enable_cross_zone_load_balancing = true
  enable_http2                     = true
  idle_timeout                     = 60

  tags = {
    "elbv2.k8s.aws/cluster"    = local.cluster_name
    "ingress.k8s.aws/resource" = "LoadBalancer"
    "ingress.k8s.aws/stack"    = "hospitalsystem/hospital-ingress"
  }

  lifecycle {
    ignore_changes = all
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.hospital_system.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-2016-08"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type  = "fixed-response"
    order = 1

    fixed_response {
      content_type = "text/plain"
      status_code  = "404"
    }
  }

  tags = {
    "elbv2.k8s.aws/cluster"    = local.cluster_name
    "ingress.k8s.aws/resource" = "443"
    "ingress.k8s.aws/stack"    = "hospitalsystem/hospital-ingress"
  }

  lifecycle {
    ignore_changes = all
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.hospital_system.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type  = "redirect"
    order = 1

    redirect {
      host        = "#{host}"
      path        = "/#{path}"
      port        = "443"
      protocol    = "HTTPS"
      query       = "#{query}"
      status_code = "HTTP_301"
    }
  }

  tags = {
    "elbv2.k8s.aws/cluster"    = local.cluster_name
    "ingress.k8s.aws/resource" = "80"
    "ingress.k8s.aws/stack"    = "hospitalsystem/hospital-ingress"
  }

  lifecycle {
    ignore_changes = all
  }
}

resource "aws_lb_target_group" "backend" {
  name                          = "k8s-hospital-hospital-2a82b88b03"
  port                          = 1
  protocol                      = "HTTP"
  protocol_version              = "HTTP1"
  target_type                   = "ip"
  vpc_id                        = aws_vpc.kubes.id
  deregistration_delay          = 300
  load_balancing_algorithm_type = "round_robin"

  health_check {
    enabled             = true
    healthy_threshold   = 2
    interval            = 15
    matcher             = "200"
    path                = "/health"
    port                = "traffic-port"
    protocol            = "HTTP"
    timeout             = 5
    unhealthy_threshold = 2
  }

  stickiness {
    cookie_duration = 86400
    type            = "lb_cookie"
  }

  tags = {
    "elbv2.k8s.aws/cluster"    = local.cluster_name
    "ingress.k8s.aws/resource" = "hospitalsystem/hospital-ingress-hospital-backend:80"
    "ingress.k8s.aws/stack"    = "hospitalsystem/hospital-ingress"
  }

  lifecycle {
    ignore_changes = all
  }
}

resource "aws_lb_target_group" "frontend" {
  name                          = "k8s-hospital-hospital-9265f59208"
  port                          = 1
  protocol                      = "HTTP"
  protocol_version              = "HTTP1"
  target_type                   = "ip"
  vpc_id                        = aws_vpc.kubes.id
  deregistration_delay          = 300
  load_balancing_algorithm_type = "round_robin"

  health_check {
    enabled             = true
    healthy_threshold   = 2
    interval            = 15
    matcher             = "200"
    path                = "/health"
    port                = "traffic-port"
    protocol            = "HTTP"
    timeout             = 5
    unhealthy_threshold = 2
  }

  stickiness {
    cookie_duration = 86400
    type            = "lb_cookie"
  }

  tags = {
    "elbv2.k8s.aws/cluster"    = local.cluster_name
    "ingress.k8s.aws/resource" = "hospitalsystem/hospital-ingress-hospital-frontend:80"
    "ingress.k8s.aws/stack"    = "hospitalsystem/hospital-ingress"
  }

  lifecycle {
    ignore_changes = all
  }
}
