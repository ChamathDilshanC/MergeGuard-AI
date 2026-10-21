# Variables from docs/DEPLOYMENT.md section 4. Defaults reproduce the project brief.
# They are declared here (T-103) and wired to resources in T-401.

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "function_name" {
  description = "Lambda function name; also determines the CloudWatch log group name."
  type        = string
  default     = "mergeguard-ai-pr-reviewer"
}

variable "lambda_timeout_seconds" {
  description = "Lambda timeout. Must stay at or below 30 while API Gateway invokes synchronously."
  type        = number
  default     = 30

  validation {
    condition     = var.lambda_timeout_seconds >= 1 && var.lambda_timeout_seconds <= 30
    error_message = "lambda_timeout_seconds must be between 1 and 30 (API Gateway HTTP API integration limit)."
  }
}

variable "lambda_memory_mb" {
  description = "Lambda memory size in MB."
  type        = number
  default     = 256

  validation {
    condition     = var.lambda_memory_mb >= 128 && var.lambda_memory_mb <= 10240
    error_message = "lambda_memory_mb must be between 128 and 10240."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the function's log group."
  type        = number
  default     = 14

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be a retention period that CloudWatch Logs accepts (for example 7, 14, 30)."
  }
}

variable "param_prefix" {
  description = "SSM Parameter Store prefix holding the three SecureString secrets (MERGEGUARD_PARAM_PREFIX)."
  type        = string
  default     = "/mergeguard"

  validation {
    condition     = can(regex("^/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$", var.param_prefix))
    error_message = "param_prefix must start with '/' and must not end with '/'."
  }
}

variable "gemini_model" {
  description = "Gemini model ID used for AI reviews (MERGEGUARD_GEMINI_MODEL)."
  type        = string
  default     = "gemini-2.5-flash"
}

variable "max_diff_chars" {
  description = "Maximum diff characters sent to the AI review (MERGEGUARD_MAX_DIFF_CHARS)."
  type        = number
  default     = 12000

  validation {
    condition     = var.max_diff_chars > 0
    error_message = "max_diff_chars must be positive."
  }
}

variable "max_files" {
  description = "Maximum number of changed files fetched per pull request (MERGEGUARD_MAX_FILES)."
  type        = number
  default     = 300

  validation {
    condition     = var.max_files > 0 && var.max_files <= 3000
    error_message = "max_files must be between 1 and 3000 (GitHub's limit for the PR files API)."
  }
}

variable "dry_run" {
  description = "Run the full pipeline without writing to GitHub (MERGEGUARD_DRY_RUN)."
  type        = bool
  default     = false
}

variable "throttling_rate_limit" {
  description = "API Gateway stage steady-state request rate limit (requests per second)."
  type        = number
  default     = 10
}

variable "throttling_burst_limit" {
  description = "API Gateway stage burst limit."
  type        = number
  default     = 20
}

variable "reserved_concurrency" {
  description = "Optional Lambda reserved concurrency cap. Null leaves the function unreserved."
  type        = number
  default     = null

  validation {
    condition     = var.reserved_concurrency == null ? true : var.reserved_concurrency >= 0
    error_message = "reserved_concurrency must be null or a non-negative number."
  }
}
