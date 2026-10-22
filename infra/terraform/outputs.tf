output "alb_dns_name" {
  description = "Public hostname of the load balancer."
  value       = aws_lb.main.dns_name
}

output "service_url" {
  description = "Base URL of the deployed service (HTTP only - see the note in alb.tf)."
  value       = "http://${aws_lb.main.dns_name}"
}

output "health_url" {
  description = "Readiness probe, the same endpoint the target group checks."
  value       = "http://${aws_lb.main.dns_name}/health"
}

output "metrics_url" {
  description = "JSON metrics snapshot from app/metrics.py."
  value       = "http://${aws_lb.main.dns_name}/metrics"
}

output "ecr_repository_url" {
  description = "Push the image here before the first apply."
  value       = aws_ecr_repository.app.repository_url
}

output "log_group" {
  description = "CloudWatch Logs group carrying the task's JSON logs."
  value       = aws_cloudwatch_log_group.tasks.name
}

output "dynamodb_dead_letters_table" {
  description = "Dead-letter records, queried by app/dynamo_dlq.py via status-seq-index."
  value       = aws_dynamodb_table.dead_letters.name
}

output "dynamodb_ledger_table" {
  description = "Idempotency ledger, written by app/dynamo_idempotency.py."
  value       = aws_dynamodb_table.ledger.name
}

output "replay_queue_url" {
  description = "SQS replay queue, or null unless enable_replay_queue is set."
  value       = var.enable_replay_queue ? aws_sqs_queue.replay[0].url : null
}

output "store_backend" {
  description = "Which storage backend the deployed tasks are configured for."
  value       = var.store_backend
}

output "deployed_account_id" {
  description = "Account the stack was applied to."
  value       = data.aws_caller_identity.current.account_id
}
