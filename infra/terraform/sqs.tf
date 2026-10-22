# Optional, and off by default.
#
# The dead-letter *records* live in DynamoDB (see dynamodb.tf) because the API needs
# to query them. SQS has a different job: a durable trigger that hands replay work to
# a consumer. Nothing in the service uses it today, so creating it by default would
# leave empty queues lying around.
#
# Turn it on when something is actually wired to consume it.

resource "aws_sqs_queue" "replay" {
  count = var.enable_replay_queue ? 1 : 0

  name                       = "${var.project}-replay"
  message_retention_seconds  = 1209600 # 14 days, the maximum
  visibility_timeout_seconds = 60
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.replay_failed[0].arn
    maxReceiveCount     = 5
  })
}

# Messages that fail repeatedly land here rather than cycling forever.
resource "aws_sqs_queue" "replay_failed" {
  count = var.enable_replay_queue ? 1 : 0

  name                      = "${var.project}-replay-failed"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue_redrive_allow_policy" "replay_failed" {
  count = var.enable_replay_queue ? 1 : 0

  queue_url = aws_sqs_queue.replay_failed[0].id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.replay[0].arn]
  })
}
