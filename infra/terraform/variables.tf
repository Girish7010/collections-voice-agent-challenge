variable "region" {
  description = "Target AWS region. The cost analysis in docs/cost-analysis.md is priced for ap-south-1."
  type        = string
  default     = "ap-south-1"
}

variable "project" {
  description = "Name prefix for all resources."
  type        = string
  default     = "collections-agent"
}

variable "allowed_account_id" {
  description = <<-EOT
    The AWS account this stack is allowed to deploy into. Terraform refuses to apply
    against any other account, which prevents an apply landing in the wrong place when
    several profiles are configured. Set it explicitly in terraform.tfvars.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.allowed_account_id))
    error_message = "allowed_account_id must be exactly 12 digits, with no dashes."
  }
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "az_count" {
  description = "How many availability zones to spread subnets across. The ALB requires at least 2."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3; an ALB needs subnets in at least two AZs."
  }
}

variable "single_nat_gateway" {
  description = <<-EOT
    true  -> one NAT gateway shared by all private subnets (~$41/mo, single point of failure).
    false -> one NAT gateway per AZ (~$41/mo each), which survives an AZ outage.
    docs/cost-analysis.md prices the single-NAT option.
  EOT
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Container image
# ---------------------------------------------------------------------------

variable "image_tag" {
  description = <<-EOT
    Tag of the image in the ECR repository this stack creates. Build and push before the
    first apply, otherwise the ECS service will have nothing to pull and will not stabilise.
    See infra/terraform/README.md for the push commands.
  EOT
  type        = string
  default     = "latest"
}

# ---------------------------------------------------------------------------
# Task sizing - configuration B from docs/cost-analysis.md
# ---------------------------------------------------------------------------

variable "task_cpu" {
  description = "Fargate CPU units. 512 = 0.5 vCPU (configuration B)."
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate memory in MiB. 1024 = 1 GB (configuration B)."
  type        = number
  default     = 1024
}

variable "desired_count" {
  description = <<-EOT
    Number of tasks to run, and the autoscaling floor.

    2 is the floor docs/cost-analysis.md recommends: one task per AZ, so the service
    survives losing an AZ, at the same cost as a single configuration-A task.

    Anything above 1 requires store_backend = "dynamodb", because the SQLite stores
    are per-task and sharing nothing. guard.tf enforces that.
  EOT
  type        = number
  default     = 2

  validation {
    condition     = var.desired_count >= 1
    error_message = "desired_count must be at least 1."
  }
}

variable "store_backend" {
  description = <<-EOT
    Where the dead-letter queue and idempotency ledger live (APP_STORE_BACKEND).

    "dynamodb" is the only sensible choice on ECS: the stores are shared, so tasks can
    scale and state survives a deployment. "sqlite" writes to the task's own ephemeral
    filesystem and is here only for a deliberate single-task deployment.
  EOT
  type        = string
  default     = "dynamodb"

  validation {
    condition     = contains(["sqlite", "dynamodb"], var.store_backend)
    error_message = "store_backend must be \"sqlite\" or \"dynamodb\"."
  }
}

variable "ledger_ttl_days" {
  description = <<-EOT
    How long a succeeded ledger record is kept (APP_LEDGER_TTL_DAYS). After this a
    duplicate of the same event_id is treated as new and processed again, so keep it
    comfortably longer than the window in which a duplicate could arrive.
    0 keeps records forever.
  EOT
  type        = number
  default     = 30
}

variable "db_threads" {
  description = <<-EOT
    Worker threads for blocking store calls (APP_DB_THREADS). DynamoDB calls are
    network round-trips, so a single thread would serialize them. Must be 1 when
    store_backend is "sqlite".
  EOT
  type        = number
  default     = 8
}

variable "enable_replay_queue" {
  description = <<-EOT
    Create the SQS replay queue and its redrive queue (see sqs.tf). Off by default:
    the dead-letter records live in DynamoDB and nothing consumes the queue yet.
  EOT
  type        = bool
  default     = false
}

variable "max_capacity" {
  description = "Autoscaling ceiling. 10 matches configuration B in docs/cost-analysis.md."
  type        = number
  default     = 10
}

# ---------------------------------------------------------------------------
# Application settings (see .env.example)
# ---------------------------------------------------------------------------

variable "dependency_url" {
  description = <<-EOT
    Base URL of the upstream STT/LLM/TTS dependency (APP_DEPENDENCY_URL).
    There is no default: pointing this at the wrong host silently sends live traffic
    somewhere unintended, so it must be stated.
  EOT
  type        = string

  validation {
    condition     = can(regex("^https?://", var.dependency_url))
    error_message = "dependency_url must start with http:// or https://."
  }
}

variable "app_settings" {
  description = <<-EOT
    Extra APP_* settings, passed verbatim as container environment variables.
    Keys must already be uppercase without the APP_ prefix, e.g. { MAX_ATTEMPTS = "3" }.
    Only names matching a field on app.config.Settings are read; anything else is ignored.
  EOT
  type        = map(string)
  default     = {}
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the task log group."
  type        = number
  default     = 30
}

variable "enable_deletion_protection" {
  description = "Deletion protection on the ALB. Turn on for anything real."
  type        = bool
  default     = false
}
