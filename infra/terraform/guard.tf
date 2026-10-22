# Refuses to plan or apply against an account other than var.allowed_account_id.
# With several credential profiles around, this is the cheapest way to stop a stack
# being created in the wrong account.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

resource "terraform_data" "account_guard" {
  input = data.aws_caller_identity.current.account_id

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.allowed_account_id
      error_message = <<-EOT
        Wrong AWS account.

        Credentials resolve to account ${data.aws_caller_identity.current.account_id},
        but allowed_account_id is set to ${var.allowed_account_id}.

        Either point your credentials at the intended account, or change
        allowed_account_id in terraform.tfvars if this account really is the target.
      EOT
    }
  }
}

# Configuration combinations that would deploy a service that corrupts its own state.
# Terraform cannot express these as variable validations, because a validation block
# may only reference its own variable.
resource "terraform_data" "config_guard" {
  input = "${var.store_backend}/${var.desired_count}"

  lifecycle {
    precondition {
      condition     = var.desired_count <= 1 || var.store_backend == "dynamodb"
      error_message = <<-EOT
        desired_count is ${var.desired_count} but store_backend is "${var.store_backend}".

        The SQLite stores live on each task's own ephemeral filesystem, so running more
        than one task would process the same event_id twice, split the dead-letter queue
        across tasks, and lose all of it on the next deployment.

        Either set store_backend = "dynamodb", or set desired_count = 1.
      EOT
    }

    precondition {
      condition     = var.store_backend != "sqlite" || var.db_threads == 1
      error_message = <<-EOT
        store_backend is "sqlite" but db_threads is ${var.db_threads}.

        Concurrent SQLite schema creation and the WAL pragma deadlock, so the store pool
        must stay at one thread. app/config.py rejects this combination at startup too.
      EOT
    }
  }
}
