terraform {
  # CI pins the exact CLI version (.github/workflows/ci.yml). 1.11 is the first
  # release with write-only arguments, which keep secrets out of state (ADR-0007).
  required_version = ">= 1.11, < 2.0"

  required_providers {
    # Constraint from the project brief. Whether to move to the current major
    # version is open question OQ-07 (docs/PRD.md section 17).
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    # Packages src/ into the Lambda zip (main.tf).
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  # No backend block: local state until OQ-08 decides on remote state.
}
