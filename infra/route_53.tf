data "aws_route53_zone" "main" {
  name = "${var.route_53_zone}"
}

resource "aws_route53_record" "ftps3_public" {
  zone_id = "${data.aws_route53_zone.main.zone_id}"
  name    = "${var.app_external_host}"
  type    = "A"

  alias {
    name                   = "${aws_lb.app_public.dns_name}"
    zone_id                = "${aws_lb.app_public.zone_id}"
    evaluate_target_health = false
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "healthcheck" {
  zone_id = "${data.aws_route53_zone.main.zone_id}"
  name    = "${var.healthcheck_host}"
  type    = "A"

  alias {
    name                   = "${aws_alb.healthcheck.dns_name}"
    zone_id                = "${aws_alb.healthcheck.zone_id}"
    evaluate_target_health = false
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate" "healthcheck" {
  domain_name       = "${aws_route53_record.healthcheck.name}"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "healthcheck" {
  certificate_arn = "${aws_acm_certificate.healthcheck.arn}"
}
