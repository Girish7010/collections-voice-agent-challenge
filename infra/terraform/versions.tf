terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Remote state. Create the bucket and lock table once, by hand, then uncomment.
  # Local state is fine for a first `plan`, but do not run a shared apply on it.
  #
  # backend "s3" {
  #   bucket         = "<your-tf-state-bucket>"
  #   key            = "collections-voice-agent/terraform.tfstate"
  #   region         = "ap-south-1"
  #   dynamodb_table = "<your-tf-lock-table>"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.region

  # Credentials come from the standard AWS credential chain (env vars, shared
  # config, SSO, or an assumed role). Never put keys in this file.

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Component = "collections-voice-agent"
    }
  }
}
