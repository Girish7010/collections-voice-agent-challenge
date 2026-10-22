# Both stores live in DynamoDB. docs/architecture.md originally proposed SQS for the
# dead-letter queue; that cannot work, because SQS has no lookup by key, no filter or
# count by status and no in-place update, so it cannot serve GET /dlq,
# GET /dlq/{event_id} or the claim/resolve lifecycle the API exposes. See the module
# docstring in app/dynamo.py.
#
# On-demand billing: the traffic in docs/cost-analysis.md (about 50 calls/min at peak)
# is far below the point where provisioned capacity is cheaper, and it needs no
# capacity planning.

# --- dead-letter records ---------------------------------------------------

resource "aws_dynamodb_table" "dead_letters" {
  name         = "${var.project}-dead-letters"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  # GSI key attributes. `seq` is a nanosecond timestamp fixed at first insert, which
  # reproduces the insertion order that SQLite's AUTOINCREMENT id gave list().
  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "seq"
    type = "N"
  }

  # app/dynamo_dlq.py queries this index once per status instead of scanning the
  # table. Statuses are a closed set (pending, replaying, resolved), so no query
  # needs a constant-value partition key.
  global_secondary_index {
    name            = "status-seq-index"
    hash_key        = "status"
    range_key       = "seq"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  # A dead letter is an event that needs a human or a replay; losing the table loses
  # that work queue.
  deletion_protection_enabled = var.enable_deletion_protection
}

# --- idempotency ledger ----------------------------------------------------

resource "aws_dynamodb_table" "ledger" {
  name         = "${var.project}-event-ledger"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  # app/dynamo_idempotency.py stamps expires_at on completion, so succeeded records
  # age out instead of growing without bound. Nothing expires while the attribute is
  # absent, which is what ledger_ttl_days = 0 produces.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = var.enable_deletion_protection
}
