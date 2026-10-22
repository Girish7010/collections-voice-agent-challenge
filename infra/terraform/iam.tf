data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    # Stops this role being assumed on behalf of a task in another account.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.allowed_account_id]
    }
  }
}

# --- execution role: used by the ECS agent, not by your code ---------------

resource "aws_iam_role" "execution" {
  name               = "${var.project}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# --- task role: used by the application itself -----------------------------

resource "aws_iam_role" "task" {
  name               = "${var.project}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task" {
  # Dead-letter records. The index ARN is listed separately because a Query against
  # status-seq-index is authorized on the index, not the table.
  statement {
    sid = "DeadLetterRecords"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]

    resources = [
      aws_dynamodb_table.dead_letters.arn,
      "${aws_dynamodb_table.dead_letters.arn}/index/*",
    ]
  }

  # Idempotency ledger. DeleteItem is required: release() drops an in-progress claim
  # so the event can be retried. No Scan, and no Query - the ledger is only ever
  # addressed by key.
  statement {
    sid = "EventLedger"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
    ]

    resources = [aws_dynamodb_table.ledger.arn]
  }

  # Only present when the replay queue is enabled; see sqs.tf.
  dynamic "statement" {
    for_each = var.enable_replay_queue ? [1] : []

    content {
      sid = "ReplayQueue"

      actions = [
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
        "sqs:ChangeMessageVisibility",
      ]

      resources = [
        aws_sqs_queue.replay[0].arn,
        aws_sqs_queue.replay_failed[0].arn,
      ]
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.project}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
