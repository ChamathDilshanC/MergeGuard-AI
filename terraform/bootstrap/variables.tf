# Inputs for the one-time bootstrap stack (T-505): the remote state bucket and
# the OIDC role that the deploy workflow assumes. Run by the maintainer only.

variable "aws_region" {
  description = "Region of the state bucket, and of the stack the deploy role manages."
  type        = string
  default     = "us-east-1"
}

variable "state_bucket_name" {
  description = "Name of the S3 bucket for Terraform state. S3 names are global, so there is no default."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid S3 bucket name (3 to 63 lower-case letters, digits, dots or hyphens)."
  }
}

variable "github_repository" {
  description = "The only repository whose workflows may assume the deploy role, as owner/name."
  type        = string
  default     = "ChamathDilshanC/MergeGuard-AI"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must look like owner/name."
  }
}

variable "plan_environment" {
  description = "GitHub environment of the plan job. It must have no required reviewers, and should be limited to the main branch."
  type        = string
  default     = "production-plan"
}

variable "apply_environment" {
  description = "GitHub environment of the apply job. It must require approval, and should be limited to the main branch."
  type        = string
  default     = "production"
}

variable "create_oidc_provider" {
  description = "Create the GitHub OIDC identity provider. An account can hold only one; set false if it already exists."
  type        = bool
  default     = true
}

variable "param_prefix" {
  description = "SSM prefix of the MergeGuard parameters (var.param_prefix of the main stack). Scopes what the deploy role may manage."
  type        = string
  default     = "/mergeguard"

  validation {
    condition     = can(regex("^/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$", var.param_prefix))
    error_message = "param_prefix must start with '/' and must not end with '/'."
  }
}

variable "state_key" {
  description = "Object key of the main stack's state file in the bucket."
  type        = string
  default     = "mergeguard/terraform.tfstate"
}
