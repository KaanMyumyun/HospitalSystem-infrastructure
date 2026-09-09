resource "aws_cloudwatch_metric_alarm" "nodegroup_no_running_nodes" {
  alarm_name          = "hospitalsystem-nodegroup-no-running-nodes"
  alarm_description   = "HospitalSystem EKS node group has no in-service instances"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "GroupInServiceInstances"
  namespace           = "AWS/AutoScaling"
  period              = 300
  statistic           = "Average"
  threshold           = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    AutoScalingGroupName = aws_eks_node_group.hospitalsystempr1.resources[0].autoscaling_groups[0].name
  }
}
