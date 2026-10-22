# docs/cost-analysis.md is explicit that the scaling signal should be calls in
# progress, not CPU, because the service spends its time waiting on the speech
# pipeline and CPU barely moves under load.
#
# The app does expose that number - in-flight, at GET /metrics - but only as JSON on
# its own port. Nothing publishes it to CloudWatch (requirements.txt has no boto3),
# so a target-tracking policy cannot read it yet.
#
# What is used here instead: ALBRequestCountPerTarget. It is a leading signal, needs
# no code changes, and is a reasonable proxy while call duration stays roughly stable.
# Once the app publishes in-flight as a custom metric, replace the policy below with
# a customized_metric_specification on it - that is the configuration the cost doc
# actually recommends.

resource "aws_appautoscaling_target" "app" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.desired_count
  max_capacity       = var.max_capacity
}

resource "aws_appautoscaling_policy" "requests" {
  name               = "${var.project}-requests-per-target"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension

  target_tracking_scaling_policy_configuration {
    # Little's law in docs/cost-analysis.md: ~67 calls in progress at peak across
    # 10 tasks is a handful of concurrent calls per task. Tune against a load test
    # (load_tests/) before trusting this number.
    target_value = 600.0

    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.main.arn_suffix}/${aws_lb_target_group.app.arn_suffix}"
    }

    # Scale out quickly for the 15-minute campaign ramp, scale in slowly so a lull
    # mid-campaign does not drop tasks that are about to be needed again.
    scale_out_cooldown = 60
    scale_in_cooldown  = 300
  }
}
