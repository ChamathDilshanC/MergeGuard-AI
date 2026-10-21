# Variables from docs/DEPLOYMENT.md section 4. Defaults reproduce the project brief.
# Declared in T-103 and used by main.tf since T-401; the Phase 2 variables (queue,
# worker, commit status, suppressions, alerts) were added in M5.

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "function_name" {
  description = "Name of the receiver Lambda (API Gateway target); also determines its CloudWatch log group name."
  type        = string
  default     = "mergeguard-ai-pr-reviewer"
}

variable "lambda_timeout_seconds" {
  description = "Receiver Lambda timeout. Must stay at or below 30: API Gateway HTTP APIs cap integrations at 30 s."
  type        = number
  default     = 30

  validation {
    condition     = var.lambda_timeout_seconds >= 1 && var.lambda_timeout_seconds <= 30
    error_message = "lambda_timeout_seconds must be between 1 and 30 (API Gateway HTTP API integration limit)."
  }
}

variable "lambda_memory_mb" {
  description = "Receiver Lambda memory size in MB."
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

variable "log_level" {
  description = "Minimum level of the Lambda's JSON logs (MERGEGUARD_LOG_LEVEL)."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR"], var.log_level)
    error_message = "log_level must be DEBUG, INFO, WARNING or ERROR."
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
  description = "Optional reserved concurrency cap for the receiver Lambda. Null leaves it unreserved."
  type        = number
  default     = null

  validation {
    condition     = var.reserved_concurrency == null ? true : var.reserved_concurrency >= 0
    error_message = "reserved_concurrency must be null or a non-negative number."
  }
}

# --- Phase 2 (M5) ------------------------------------------------------------------

variable "worker_function_name" {
  description = "Name of the SQS worker Lambda; also determines its CloudWatch log group name."
  type        = string
  default     = "mergeguard-ai-pr-worker"
}

variable "worker_timeout_seconds" {
  description = "Worker Lambda timeout. It can exceed 30 s because API Gateway no longer waits for it (ADR-0005). The queue's visibility timeout is six times this value."
  type        = number
  default     = 60

  validation {
    condition     = var.worker_timeout_seconds >= 10 && var.worker_timeout_seconds <= 900
    error_message = "worker_timeout_seconds must be between 10 and 900 (the Lambda maximum)."
  }
}

variable "worker_memory_mb" {
  description = "Worker Lambda memory size in MB."
  type        = number
  default     = 256

  validation {
    condition     = var.worker_memory_mb >= 128 && var.worker_memory_mb <= 10240
    error_message = "worker_memory_mb must be between 128 and 10240."
  }
}

variable "worker_max_concurrency" {
  description = "Maximum concurrent worker invocations (event source mapping scaling). Caps Gemini and GitHub traffic without needing reserved concurrency (SECURITY.md TM-08, TM-13)."
  type        = number
  default     = 2

  validation {
    condition     = var.worker_max_concurrency >= 2 && var.worker_max_concurrency <= 1000
    error_message = "worker_max_concurrency must be between 2 (the minimum Lambda accepts) and 1000."
  }
}

variable "queue_max_receive_count" {
  description = "How many times SQS hands a review request to the worker before it moves to the dead-letter queue."
  type        = number
  default     = 3

  validation {
    condition     = var.queue_max_receive_count >= 1 && var.queue_max_receive_count <= 10
    error_message = "queue_max_receive_count must be between 1 and 10."
  }
}

variable "commit_status_enabled" {
  description = "Set the mergeguard/review commit status (MERGEGUARD_COMMIT_STATUS). The GitHub token then needs the Commit statuses: Read and write permission (DEPLOYMENT.md section 3.1)."
  type        = bool
  default     = true
}

variable "scan_allow_paths" {
  description = "Path globs whose secret-scan findings are suppressed and listed in the review (MERGEGUARD_SCAN_ALLOW_PATHS, FR-019), for example tests/fixtures/**. Operator-defined: a pull request cannot change it."
  type        = list(string)
  default     = []

  # One condition per line: checkov's HCL parser cannot read a condition that
  # continues across lines inside a for expression.
  validation {
    condition     = alltrue([for pattern in var.scan_allow_paths : can(regex("^[A-Za-z0-9_./*?@+=~ -]{1,200}$", pattern))])
    error_message = "Each scan_allow_paths entry must be at most 200 characters of path characters, without commas."
  }

  validation {
    condition     = alltrue([for pattern in var.scan_allow_paths : !startswith(pattern, "/") && !strcontains(pattern, "..")])
    error_message = "Each scan_allow_paths entry must be a relative path glob without '..'."
  }

  validation {
    condition     = alltrue([for pattern in var.scan_allow_paths : trim(pattern, "*?/") != ""])
    error_message = "Each scan_allow_paths entry must name a path: a bare wildcard would switch the scanner off."
  }

  validation {
    condition     = length(var.scan_allow_paths) <= 50
    error_message = "scan_allow_paths allows at most 50 patterns."
  }
}

variable "alert_email" {
  description = "Email address that receives alarm and budget notifications. Null creates the alarms without a subscriber. The subscription must be confirmed from the email AWS sends."
  type        = string
  default     = null

  validation {
    condition     = var.alert_email == null ? true : can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address or null."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly AWS cost budget in USD (NFR-007). Null creates no budget: the amount is the owner's decision (OQ-05). Requires alert_email."
  type        = number
  default     = null

  validation {
    condition     = var.monthly_budget_usd == null ? true : var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be null or positive."
  }
}

# --- Phase 3 (M6) ------------------------------------------------------------------

variable "github_app_id" {
  description = "ID of the GitHub App that MergeGuard authenticates as (MERGEGUARD_GITHUB_APP_ID, FR-020). Null keeps the personal access token. The App's private key goes in the github_app_private_key SSM parameter (docs/DEPLOYMENT.md section 3.4); an App ID is not a secret."
  type        = string
  default     = null

  validation {
    condition     = var.github_app_id == null ? true : can(regex("^[A-Za-z0-9._-]{1,40}$", var.github_app_id))
    error_message = "github_app_id must be a GitHub App ID or client ID, or null."
  }
}

variable "inline_comments" {
  description = "Inline line-level review comments (MERGEGUARD_INLINE_COMMENTS, FR-021): off, secrets (secret findings only) or all (also AI findings). all uses the longer review-v2 prompt, which has not been evaluated (T-304, OQ-05), so the default stops at secrets."
  type        = string
  default     = "secrets"

  validation {
    condition     = contains(["off", "secrets", "all"], var.inline_comments)
    error_message = "inline_comments must be off, secrets or all."
  }
}

variable "repo_config_enabled" {
  description = "Read .github/mergeguard.json from each pull request's base commit (MERGEGUARD_REPO_CONFIG, FR-022). Needs the Contents: read permission."
  type        = bool
  default     = true
}

variable "repo_config_block_levels" {
  description = "The lowest AI risk levels at which a repository file may choose to request changes (MERGEGUARD_REPO_CONFIG_BLOCK_LEVELS). Use [\"MEDIUM\"] to forbid relaxing the default."
  type        = list(string)
  default     = ["MEDIUM", "HIGH"]

  validation {
    condition     = length(var.repo_config_block_levels) > 0 && alltrue([for level in var.repo_config_block_levels : contains(["LOW", "MEDIUM", "HIGH"], level)])
    error_message = "repo_config_block_levels must be a non-empty list of LOW, MEDIUM or HIGH."
  }
}
