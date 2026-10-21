# MergeGuard AI stack (T-401): SSM parameter placeholders, IAM, Lambda,
# CloudWatch log groups and the HTTP API. See docs/DEPLOYMENT.md.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  api_name         = "mergeguard-webhook-gateway"
  lambda_log_group = "/aws/lambda/${var.function_name}"
  api_log_group    = "/aws/apigateway/${local.api_name}"

  # The parameters the Lambda reads (docs/API_SPEC.md section 5).
  secret_names = toset(["github_token", "webhook_secret", "gemini_api_key"])

  # Public, non-secret value Terraform writes once (ADR-0007). The Lambda treats
  # any value with this prefix as "not set" (config.PLACEHOLDER_PREFIX).
  secret_placeholder = "mergeguard-placeholder:set-with-aws-ssm-put-parameter"
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
  value_wo         = local.secret_placeholder
  value_wo_version = 1
}

# --- IAM: exactly docs/SECURITY.md section 5.3 ---------------------------------

resource "aws_iam_role" "lambda" {
  name        = "mergeguard_lambda_exec_role"
  description = "Execution role for the MergeGuard AI reviewer Lambda."

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
        Sid      = "ReadOwnParameters"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.param_prefix}/*"
      },
    ]
  })
}

# --- Logs --------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda" {
  #checkov:skip=CKV_AWS_158:Allow-listed metadata only, never secrets or code (FR-013); CloudWatch Logs already encrypts it at rest, a CMK adds cost and a key policy.
  #checkov:skip=CKV_AWS_338:14-day retention is the documented default for metadata-only logs (DEPLOYMENT.md 4); raise log_retention_days if an audit needs a year.
  name              = local.lambda_log_group
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
  #checkov:skip=CKV_AWS_116:API Gateway invokes the function synchronously, so a DLQ never receives anything; the Phase 2 SQS design adds one (ADR-0005).
  #checkov:skip=CKV_AWS_117:The function only calls GitHub, Gemini and SSM over HTTPS and reaches no private resources; a VPC would add NAT cost and no protection.
  #checkov:skip=CKV_AWS_173:Environment variables hold non-secret settings only (ARCHITECTURE.md 10); secrets come from SSM at runtime. Lambda encrypts them at rest by default.
  #checkov:skip=CKV_AWS_272:Phase 1 deploys a zip that Terraform builds from a reviewed commit; code signing is revisited with the CI deploy pipeline (T-505).
  function_name    = var.function_name
  description      = "MergeGuard AI: GitHub webhook, secret scan and Gemini review."
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = var.lambda_timeout_seconds
  memory_size      = var.lambda_memory_mb

  # -1 leaves the function unreserved; see var.reserved_concurrency.
  reserved_concurrent_executions = coalesce(var.reserved_concurrency, -1)

  # Non-secret settings only (docs/ARCHITECTURE.md section 10).
  environment {
    variables = {
      MERGEGUARD_PARAM_PREFIX   = var.param_prefix
      MERGEGUARD_GEMINI_MODEL   = var.gemini_model
      MERGEGUARD_MAX_DIFF_CHARS = tostring(var.max_diff_chars)
      MERGEGUARD_MAX_FILES      = tostring(var.max_files)
      MERGEGUARD_LOG_LEVEL      = var.log_level
      MERGEGUARD_DRY_RUN        = tostring(var.dry_run)
    }
  }

  # The log group must exist first, or Lambda creates one without retention.
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda]
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
