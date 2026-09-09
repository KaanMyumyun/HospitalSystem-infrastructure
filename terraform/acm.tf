resource "aws_acm_certificate" "app" {
  domain_name       = var.app_domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

locals {
  acm_validation_record = {
    for option in aws_acm_certificate.app.domain_validation_options : option.domain_name => {
      name  = option.resource_record_name
      type  = option.resource_record_type
      value = option.resource_record_value
    }
  }[var.app_domain_name]
}

data "cloudflare_zones" "app" {
  name = var.cloudflare_zone_name
}

resource "cloudflare_dns_record" "acm_validation" {
  zone_id = data.cloudflare_zones.app.result[0].id
  name    = trimsuffix(local.acm_validation_record.name, ".")
  type    = local.acm_validation_record.type
  content = trimsuffix(local.acm_validation_record.value, ".")
  ttl     = 60
  proxied = false
}

resource "aws_acm_certificate_validation" "app" {
  certificate_arn         = aws_acm_certificate.app.arn
  validation_record_fqdns = [local.acm_validation_record.name]
}
