# MergeGuard AI stack: SSM parameter placeholders, IAM, the receiver and worker
# Lambdas, the review queue with its dead-letter queue, CloudWatch log groups and
# the HTTP API (T-401, T-501). Alarms and the budget are in alerts.tf (T-506).
# See docs/DEPLOYMENT.md.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  api_name         = "mergeguard-webhook-gateway"
  lambda_log_group = "/aws/lambda/${var.function_name}"
  worker_log_group = "/aws/lambda/${var.worker_function_name}"
  api_log_group    = "/aws/apigateway/${local.api_name}"

  # The queue ARN is built from its name, so the IAM policies are known at plan
  # time and the plan tests can assert them exactly.
  queue_name = "mergeguard-review-requests"
  dlq_name   = "${local.queue_name}-dlq"
  queue_arn  = "arn:aws:sqs:${local.region}:${local.account_id}:${local.queue_name}"
  dlq_arn    = "arn:aws:sqs:${local.region}:${local.account_id}:${local.dlq_name}"

  # SQS must keep a message invisible for longer than one worker run, or a slow
  # review is handed out twice. Six times the timeout is AWS's recommendation.
  queue_visibility_timeout = max(30, var.worker_timeout_seconds * 6)

  # The parameters the Lambda reads (docs/API_SPEC.md section 5).
  secret_names = toset(["github_token", "webhook_secret", "gemini_api_key"])

  # Public, non-secret value Terraform writes once (ADR-0007). The Lambda treats
  # any value with this prefix as "not set" (config.PLACEHOLDER_PREFIX).
  parameter_placeholder = "mergeguard-placeholder:set-with-aws-ssm-put-parameter"
}

# --- SSM parameter placeholders (ADR-0007) -------------------------------------

resource "aws_ssm_parameter" "secret" {
  #checkov:skip=CKV_AWS_337:ADR-0003 keeps the AWS-managed aws/ssm key; a customer-managed key adds cost and a kms:Decrypt grant outside SECURITY.md 5.3.
  for_each = local.secret_names

  name        = "${var.param_prefix}/${each.key}"
  description = "MergeGuard ${each.key}. Terraform writes only a placeholder; set the real value with aws ssm put-parameter --overwrite (DEPLOYMENT.md section 3)."
  type        = "SecureString"

  # Write-only: never stored in state or plan, and only re-sent when the version
  # changes, so real values set with the AWS CLI are never read into state.
  value_wo         = local.parameter_placeholder
  value_wo_version = 1
}

# --- IAM: exactly docs/SECURITY.md section 5.3 ---------------------------------
# Two roles, so each function can do only its own job: the receiver reads the
# webhook secret and queues; the worker reads the GitHub token and the Gemini key
# and consumes the queue.

resource "aws_iam_role" "lambda" {
  name        = "mergeguard_lambda_exec_role"
  description = "Execution role for the MergeGuard AI receiver Lambda."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      },
    ]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "mergeguard_lambda_policy"
  role = aws_iam_role.lambda.id

  # ARNs come from data sources; no wildcard account or region.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:log-group:${local.lambda_log_group}:*"
      },
      {
        Sid      = "ReadWebhookSecret"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.param_prefix}/webhook_secret"
      },
      {
        Sid      = "QueueReviewRequests"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = local.queue_arn
      },
    ]
  })
}

resource "aws_iam_role" "worker" {
  name        = "mergeguard_worker_exec_role"
  description = "Execution role for the MergeGuard AI worker Lambda."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      },
    ]
  })
}

resource "aws_iam_role_policy" "worker" {
  name = "mergeguard_worker_policy"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:log-group:${local.worker_log_group}:*"
      },
      {
        Sid    = "ReadGitHubTokenAndGeminiKey"
        Effect = "Allow"
        Action = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = [
          "arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.param_prefix}/github_token",
          "arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.param_prefix}/gemini_api_key",
        ]
      },
      {
        Sid      = "ConsumeReviewRequests"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = local.queue_arn
      },
    ]
  })
}

# --- Review queue and dead-letter queue (ADR-0005) -------------------------------
# Messages carry metadata only (docs/API_SPEC.md section 6), never code.

resource "aws_sqs_queue" "dlq" {
  name                      = local.dlq_name
  message_retention_seconds = 1209600 # 14 days, the SQS maximum: time to look and redrive
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "review_requests" {
  name                       = local.queue_name
  visibility_timeout_seconds = local.queue_visibility_timeout
  message_retention_seconds  = 86400 # a review request older than a day is stale
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = local.dlq_arn
    maxReceiveCount     = var.queue_max_receive_count
  })

  # SQS refuses a redrive policy whose dead-letter queue does not exist yet.
  depends_on = [aws_sqs_queue.dlq]
}

# Only this queue may use the dead-letter queue.
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [local.queue_arn]
  })
}

# --- Logs --------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda" {
  #checkov:skip=CKV_AWS_158:Allow-listed metadata only, never secrets or code (FR-013); CloudWatch Logs already encrypts it at rest, a CMK adds cost and a key policy.
  #checkov:skip=CKV_AWS_338:14-day retention is the documented default for metadata-only logs (DEPLOYMENT.md 4); raise log_retention_days if an audit needs a year.
  name              = local.lambda_log_group
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  #checkov:skip=CKV_AWS_158:Allow-listed metadata only, never secrets or code (FR-013); CloudWatch Logs already encrypts it at rest, a CMK adds cost and a key policy.
  #checkov:skip=CKV_AWS_338:14-day retention is the documented default for metadata-only logs (DEPLOYMENT.md 4); raise log_retention_days if an audit needs a year.
  name              = local.worker_log_group
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "api_access" {
  #checkov:skip=CKV_AWS_158:Request metadata only (no bodies or headers with secrets); CloudWatch Logs already encrypts it at rest, a CMK adds cost and a key policy.
  #checkov:skip=CKV_AWS_338:14-day retention is the documented default for metadata-only logs (DEPLOYMENT.md 4); raise log_retention_days if an audit needs a year.
  name              = local.api_log_group
  retention_in_days = var.log_retention_days
}

# --- Lambda ------------------------------------------------------------------------

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/build/lambda.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
  # Fixed file mode, so the same source gives the same hash on every OS.
  output_file_mode = "0644"
}

resource "aws_lambda_function" "reviewer" {
  #checkov:skip=CKV_AWS_50:Tracing is not planned for Phase 1 (ARCHITECTURE.md 12) and would need X-Ray permissions outside the SECURITY.md 5.3 policy.
  #checkov:skip=CKV_AWS_115:Reserved concurrency is optional (DEPLOYMENT.md 4) because new accounts cannot reserve any; stage throttling caps load (TM-08). Set reserved_concurrency to enable.
  #checkov:skip=CKV_AWS_116:API Gateway invokes the receiver synchronously, so a Lambda DLQ never receives anything; failed work is retried by SQS and ends in the review queue's dead-letter queue (ADR-0005), which has an alarm.
  #checkov:skip=CKV_AWS_117:The function only calls SQS and SSM over HTTPS and reaches no private resources; a VPC would add NAT cost and no protection.
  #checkov:skip=CKV_AWS_173:Environment variables hold non-secret settings only (ARCHITECTURE.md 10); secrets come from SSM at runtime. Lambda encrypts them at rest by default.
  #checkov:skip=CKV_AWS_272:The zip is built by Terraform from a reviewed commit and deployed from CI behind an approval; code signing needs a signing profile outside this stack (T-505, SECURITY.md 5.9).
  function_name    = var.function_name
  description      = "MergeGuard AI receiver: verifies the GitHub webhook and queues a review request."
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = var.lambda_timeout_seconds
  memory_size      = var.lambda_memory_mb

  # -1 leaves the function unreserved; see var.reserved_concurrency.
  reserved_concurrent_executions = coalesce(var.reserved_concurrency, -1)

  # Non-secret settings only (docs/ARCHITECTURE.md section 10). The queue URL
  # makes this function the receiver; the review settings belong to the worker.
  environment {
    variables = {
      MERGEGUARD_PARAM_PREFIX = var.param_prefix
      MERGEGUARD_LOG_LEVEL    = var.log_level
      MERGEGUARD_QUEUE_URL    = aws_sqs_queue.review_requests.url
    }
  }

  # The log group must exist first, or Lambda creates one without retention.
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda]
}

# --- Worker Lambda: SQS -> fetch, scan, AI, publish (ADR-0005) -------------------

resource "aws_lambda_function" "worker" {
  #checkov:skip=CKV_AWS_50:Tracing is not planned (ARCHITECTURE.md 12) and would need X-Ray permissions outside the SECURITY.md 5.3 policy.
  #checkov:skip=CKV_AWS_115:Concurrency is capped by the event source mapping (worker_max_concurrency), which needs no account quota; reserved concurrency is not available on new accounts.
  #checkov:skip=CKV_AWS_116:SQS invokes the worker, and the queue's redrive policy sends exhausted requests to the dead-letter queue (ADR-0005), which has an alarm. A Lambda DLQ only applies to asynchronous invocations.
  #checkov:skip=CKV_AWS_117:The function only calls GitHub, Gemini and SSM over HTTPS and reaches no private resources; a VPC would add NAT cost and no protection.
  #checkov:skip=CKV_AWS_173:Environment variables hold non-secret settings only (ARCHITECTURE.md 10); secrets come from SSM at runtime. Lambda encrypts them at rest by default.
  #checkov:skip=CKV_AWS_272:The zip is built by Terraform from a reviewed commit and deployed from CI behind an approval; code signing needs a signing profile outside this stack (T-505, SECURITY.md 5.9).
  function_name    = var.worker_function_name
  description      = "MergeGuard AI worker: secret scan, Gemini review, commit status and stale-review dismissal."
  role             = aws_iam_role.worker.arn
  handler          = "worker_function.lambda_handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = var.worker_timeout_seconds
  memory_size      = var.worker_memory_mb

  environment {
    variables = {
      MERGEGUARD_PARAM_PREFIX     = var.param_prefix
      MERGEGUARD_GEMINI_MODEL     = var.gemini_model
      MERGEGUARD_MAX_DIFF_CHARS   = tostring(var.max_diff_chars)
      MERGEGUARD_MAX_FILES        = tostring(var.max_files)
      MERGEGUARD_LOG_LEVEL        = var.log_level
      MERGEGUARD_DRY_RUN          = tostring(var.dry_run)
      MERGEGUARD_COMMIT_STATUS    = tostring(var.commit_status_enabled)
      MERGEGUARD_SCAN_ALLOW_PATHS = join(",", var.scan_allow_paths)
    }
  }

  depends_on = [aws_cloudwatch_log_group.worker, aws_iam_role_policy.worker]
}

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn = aws_sqs_queue.review_requests.arn
  function_name    = aws_lambda_function.worker.arn

  # One request per invocation: each makes its own Gemini call, and a failure
  # then retries only that request.
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = var.worker_max_concurrency
  }

  # The role needs the queue permissions before the mapping can be created.
  depends_on = [aws_iam_role_policy.worker]
}

# --- HTTP API: POST /webhook only ------------------------------------------------

resource "aws_apigatewayv2_api" "webhook" {
  name          = local.api_name
  description   = "GitHub webhook receiver for MergeGuard AI."
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_stage" "default" {
  #checkov:skip=CKV2_AWS_51:Client certificates authenticate API Gateway to HTTP backends and apply only to WebSocket APIs; this HTTP API calls Lambda directly.
  api_id      = aws_apigatewayv2_api.webhook.id
  name        = "$default"
  auto_deploy = true

  # Cost and abuse guard for the public endpoint (SECURITY.md TM-08).
  default_route_settings {
    throttling_rate_limit  = var.throttling_rate_limit
    throttling_burst_limit = var.throttling_burst_limit
  }

  # Request metadata only; API Gateway access logs never contain the body.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId        = "$context.requestId"
      requestTime      = "$context.requestTime"
      sourceIp         = "$context.identity.sourceIp"
      userAgent        = "$context.identity.userAgent"
      routeKey         = "$context.routeKey"
      status           = "$context.status"
      responseLength   = "$context.responseLength"
      integrationError = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.webhook.id
  integration_type       = "AWS_PROXY"
  integration_method     = "POST"
  integration_uri        = aws_lambda_function.reviewer.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = var.lambda_timeout_seconds * 1000
}

resource "aws_apigatewayv2_route" "webhook" {
  #checkov:skip=CKV_AWS_309:GitHub webhooks cannot send IAM or JWT credentials; the Lambda verifies every request's HMAC-SHA256 signature before parsing it (FR-002, TM-01).
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "POST /webhook"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowWebhookRouteInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reviewer.function_name
  principal     = "apigateway.amazonaws.com"
  # Only POST /webhook on this API, in any stage.
  source_arn = "${aws_apigatewayv2_api.webhook.execution_arn}/*/POST/webhook"
}
