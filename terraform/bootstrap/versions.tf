terraform {
  # Same pins as the main stack (terraform/versions.tf): the CLI version is set
  # in CI, and the AWS provider constraint is the brief's (open question OQ-07).
  required_version = ">= 1.11, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # No backend block, on purpose: this stack creates the state bucket, so it
  # cannot store its own state there. It keeps a small local state file that the
  # maintainer holds (docs/DEPLOYMENT.md section 8).
}
