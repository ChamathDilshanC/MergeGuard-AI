# Credential-free plan checks (T-401). The AWS provider is mocked, so these
# runs use no AWS account, credentials or AWS API calls. Run with:
#   terraform -chdir=terraform test

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_region" {
    defaults = { name = "us-east-1" }
  }
}

run "default_stack" {
  command = plan

  # IAM: exactly docs/SECURITY.md section 5.3.
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.lambda.policy).Statement) == 2
    error_message = "The Lambda policy must have exactly two statements."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.lambda.policy).Statement[0].Action == [
      "logs:CreateLogStream", "logs:PutLogEvents",
    ]
    error_message = "Log permissions differ from SECURITY.md 5.3."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.lambda.policy).Statement[0].Resource == "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/mergeguard-ai-pr-reviewer:*"
    error_message = "Log permissions must be scoped to the function's own log group."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.lambda.policy).Statement[1].Action == [
      "ssm:GetParameter", "ssm:GetParameters",
    ]
    error_message = "SSM permissions differ from SECURITY.md 5.3."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.lambda.policy).Statement[1].Resource == "arn:aws:ssm:us-east-1:123456789012:parameter/mergeguard/*"
    error_message = "SSM permissions must be scoped to /mergeguard/* in this account and region."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.lambda.policy).Statement :
      !anytrue([for action in statement.Action : strcontains(action, "*")])
    ])
    error_message = "No wildcard actions are allowed."
  }
  assert {
    condition     = jsondecode(aws_iam_role.lambda.assume_role_policy).Statement[0].Principal.Service == "lambda.amazonaws.com"
    error_message = "Only Lambda may assume the execution role."
  }

  # SSM: three SecureString placeholders, value written write-only.
  assert {
    condition     = sort([for p in aws_ssm_parameter.secret : p.name]) == tolist(["/mergeguard/gemini_api_key", "/mergeguard/github_token", "/mergeguard/webhook_secret"])
    error_message = "Unexpected SSM parameter names."
  }
  assert {
    condition     = alltrue([for p in aws_ssm_parameter.secret : p.type == "SecureString" && p.value_wo_version == 1])
    error_message = "Parameters must be SecureString with a write-only placeholder."
  }

  # Lambda.
  assert {
    condition     = aws_lambda_function.reviewer.runtime == "python3.12" && aws_lambda_function.reviewer.handler == "lambda_function.lambda_handler"
    error_message = "Runtime or handler differs from the docs."
  }
  assert {
    condition     = aws_lambda_function.reviewer.timeout == 30 && aws_lambda_function.reviewer.memory_size == 256
    error_message = "Timeout or memory differs from the brief's defaults."
  }
  assert {
    condition     = aws_lambda_function.reviewer.reserved_concurrent_executions == -1
    error_message = "Reserved concurrency must be unset by default."
  }
  assert {
    condition = aws_lambda_function.reviewer.environment[0].variables == tomap({
      MERGEGUARD_PARAM_PREFIX   = "/mergeguard"
      MERGEGUARD_GEMINI_MODEL   = "gemini-2.5-flash"
      MERGEGUARD_MAX_DIFF_CHARS = "12000"
      MERGEGUARD_MAX_FILES      = "300"
      MERGEGUARD_LOG_LEVEL      = "INFO"
      MERGEGUARD_DRY_RUN        = "false"
    })
    error_message = "Lambda environment differs from ARCHITECTURE.md section 10."
  }

  # Logs.
  assert {
    condition     = aws_cloudwatch_log_group.lambda.name == "/aws/lambda/mergeguard-ai-pr-reviewer" && aws_cloudwatch_log_group.lambda.retention_in_days == 14
    error_message = "The Lambda log group must exist with 14-day retention."
  }
  assert {
    condition     = aws_cloudwatch_log_group.api_access.retention_in_days == 14
    error_message = "API access logs must have the same retention."
  }

  # HTTP API.
  assert {
    condition     = aws_apigatewayv2_route.webhook.route_key == "POST /webhook"
    error_message = "The only route must be POST /webhook."
  }
  assert {
    condition     = aws_apigatewayv2_stage.default.default_route_settings[0].throttling_rate_limit == 10 && aws_apigatewayv2_stage.default.default_route_settings[0].throttling_burst_limit == 20
    error_message = "Stage throttling must default to 10 requests/s, burst 20."
  }
  assert {
    condition     = aws_apigatewayv2_integration.lambda.payload_format_version == "2.0" && aws_apigatewayv2_integration.lambda.timeout_milliseconds == 30000
    error_message = "Integration must use payload 2.0 and the Lambda timeout."
  }
  assert {
    condition     = aws_lambda_permission.api.principal == "apigateway.amazonaws.com"
    error_message = "Only API Gateway may invoke the function."
  }
}

run "custom_prefix_moves_parameters_and_iam_scope" {
  command = plan

  variables {
    param_prefix = "/team/mergeguard"
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.lambda.policy).Statement[1].Resource == "arn:aws:ssm:us-east-1:123456789012:parameter/team/mergeguard/*"
    error_message = "The SSM scope must follow param_prefix."
  }
  assert {
    condition     = aws_ssm_parameter.secret["github_token"].name == "/team/mergeguard/github_token"
    error_message = "Parameter names must follow param_prefix."
  }
}

run "reserved_concurrency_can_be_capped" {
  command = plan

  variables {
    reserved_concurrency = 5
  }

  assert {
    condition     = aws_lambda_function.reviewer.reserved_concurrent_executions == 5
    error_message = "reserved_concurrency must reach the function."
  }
}

run "rejects_timeout_above_api_gateway_limit" {
  command = plan

  variables {
    lambda_timeout_seconds = 31
  }

  expect_failures = [var.lambda_timeout_seconds]
}
