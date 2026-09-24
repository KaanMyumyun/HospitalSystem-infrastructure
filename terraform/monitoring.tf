locals {
  # kms.tf needs the topic ARN in the key policy, and the topic needs the key,
  # so the ARN is built from the name instead of read from the resource.
  alerts_topic_name = "${var.project_name}-alerts"
  alerts_topic_arn  = "arn:aws:sns:${var.aws_region}:${local.account_id}:${local.alerts_topic_name}"
  node_asg_name     = aws_eks_node_group.hospitalsystempr1.resources[0].autoscaling_groups[0].name
}

resource "aws_sns_topic" "alerts" {
  name              = local.alerts_topic_name
  kms_master_key_id = aws_kms_key.main.arn
}

# AWS emails a confirmation link, and nothing is delivered until it's clicked.
# Every new topic (so every rebuild) needs a new confirmation.
resource "aws_sns_topic_subscription" "alert_email" {
  count = var.alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# EKS creates the node group's Auto Scaling group, so Terraform can't set its
# enabled_metrics. Group metrics at one-minute granularity are free.
resource "terraform_data" "nodegroup_metrics" {
  triggers_replace = [local.node_asg_name]

  provisioner "local-exec" {
    command = "aws autoscaling enable-metrics-collection --region \"$REGION\" --auto-scaling-group-name \"$ASG\" --granularity 1Minute --metrics GroupDesiredCapacity GroupInServiceInstances"

    environment = {
      REGION = var.aws_region
      ASG    = local.node_asg_name
    }
  }
}

# Compares in-service nodes with the desired count, so scaling the node group
# to 0 on purpose stays OK while nodes that fail to launch or stay unhealthy
# alarm after 15 minutes.
resource "aws_cloudwatch_metric_alarm" "nodegroup_missing_nodes" {
  alarm_name          = "${var.project_name}-nodegroup-missing-nodes"
  alarm_description   = "HospitalSystem EKS node group has had fewer in-service nodes than desired for 15 minutes"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 3
  threshold           = 1
  # No data means group metrics are off: show INSUFFICIENT_DATA, not a false OK.
  treat_missing_data = "missing"
  alarm_actions      = [aws_sns_topic.alerts.arn]
  ok_actions         = [aws_sns_topic.alerts.arn]

  metric_query {
    id          = "missing"
    expression  = "desired - in_service"
    label       = "Desired nodes not in service"
    return_data = true
  }

  metric_query {
    id = "desired"

    metric {
      namespace   = "AWS/AutoScaling"
      metric_name = "GroupDesiredCapacity"
      period      = 300
      stat        = "Average"
      dimensions  = { AutoScalingGroupName = local.node_asg_name }
    }
  }

  metric_query {
    id = "in_service"

    metric {
      namespace   = "AWS/AutoScaling"
      metric_name = "GroupInServiceInstances"
      period      = 300
      stat        = "Average"
      dimensions  = { AutoScalingGroupName = local.node_asg_name }
    }
  }

  depends_on = [terraform_data.nodegroup_metrics]
}
