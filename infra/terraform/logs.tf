resource "aws_cloudwatch_log_group" "tasks" {
  name              = "/ecs/${var.project}"
  retention_in_days = var.log_retention_days
}
