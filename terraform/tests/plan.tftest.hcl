# Credential-free plan checks (T-401, T-501, T-506). The AWS provider is mocked,
# so these runs use no AWS account, credentials or AWS API calls. Run with:
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

  # IAM, receiver: exactly docs/SECURITY.md section 5.3.
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.lambda.policy).Statement) == 3
    error_message = "The receiver policy must have exactly three statements."
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
    condition     = jsondecode(aws_iam_role_policy.lambda.policy).Statement[1].Resource == "arn:aws:ssm:us-east-1:123456789012:parameter/mergeguard/webhook_secret"
    error_message = "The receiver may read the webhook secret only."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.lambda.policy).Statement[2].Action == ["sqs:SendMessage"]
    error_message = "The receiver may only send to the review queue."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.lambda.policy).Statement[2].Resource == "arn:aws:sqs:us-east-1:123456789012:mergeguard-review-requests"
    error_message = "The receiver's queue permission must name the review queue."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.lambda.policy).Statement :
      !anytrue([for action in statement.Action : strcontains(action, "*")])
    ])
    error_message = "No wildcard actions are allowed (receiver)."
  }
  assert {
    condition     = jsondecode(aws_iam_role.lambda.assume_role_policy).Statement[0].Principal.Service == "lambda.amazonaws.com"
    error_message = "Only Lambda may assume the receiver role."
  }

  # IAM, worker: the token and the Gemini key, and the queue it consumes.
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.worker.policy).Statement) == 3
    error_message = "The worker policy must have exactly three statements."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.worker.policy).Statement[0].Resource == "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/mergeguard-ai-pr-worker:*"
    error_message = "The worker may write only its own log group."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.worker.policy).Statement[1].Resource == [
      "arn:aws:ssm:us-east-1:123456789012:parameter/mergeguard/github_token",
      "arn:aws:ssm:us-east-1:123456789012:parameter/mergeguard/gemini_api_key",
    ]
    error_message = "The worker may read the GitHub token and the Gemini key only, never the webhook secret."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.worker.policy).Statement[2].Action == [
      "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
    ]
    error_message = "The worker's queue permissions differ from the documented set."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.worker.policy).Statement[2].Resource == "arn:aws:sqs:us-east-1:123456789012:mergeguard-review-requests"
    error_message = "The worker may consume the review queue only."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.worker.policy).Statement :
      !anytrue([for action in statement.Action : strcontains(action, "*")])
    ])
    error_message = "No wildcard actions are allowed (worker)."
  }
  assert {
    condition     = jsondecode(aws_iam_role.worker.assume_role_policy).Statement[0].Principal.Service == "lambda.amazonaws.com"
    error_message = "Only Lambda may assume the worker role."
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

  # Receiver Lambda.
  assert {
    condition     = aws_lambda_function.reviewer.runtime == "python3.12" && aws_lambda_function.reviewer.handler == "lambda_function.lambda_handler"
    error_message = "Receiver runtime or handler differs from the docs."
  }
  assert {
    condition     = aws_lambda_function.reviewer.timeout == 30 && aws_lambda_function.reviewer.memory_size == 256
    error_message = "Receiver timeout or memory differs from the defaults."
  }
  assert {
    condition     = aws_lambda_function.reviewer.reserved_concurrent_executions == -1
    error_message = "Reserved concurrency must be unset by default."
  }
  assert {
    condition     = toset(keys(aws_lambda_function.reviewer.environment[0].variables)) == toset(["MERGEGUARD_LOG_LEVEL", "MERGEGUARD_PARAM_PREFIX", "MERGEGUARD_QUEUE_URL"])
    error_message = "The receiver's environment must hold only the prefix, log level and queue URL."
  }

  # Worker Lambda.
  assert {
    condition     = aws_lambda_function.worker.runtime == "python3.12" && aws_lambda_function.worker.handler == "worker_function.lambda_handler"
    error_message = "Worker runtime or handler differs from the docs."
  }
  assert {
    condition     = aws_lambda_function.worker.timeout == 60 && aws_lambda_function.worker.memory_size == 256
    error_message = "Worker timeout or memory differs from the defaults."
  }
  assert {
    condition     = aws_lambda_function.worker.source_code_hash == aws_lambda_function.reviewer.source_code_hash
    error_message = "Both functions must run the same code package (ADR-0005)."
  }
  assert {
    condition = aws_lambda_function.worker.environment[0].variables == tomap({
      MERGEGUARD_PARAM_PREFIX     = "/mergeguard"
      MERGEGUARD_GEMINI_MODEL     = "gemini-2.5-flash"
      MERGEGUARD_MAX_DIFF_CHARS   = "12000"
      MERGEGUARD_MAX_FILES        = "300"
      MERGEGUARD_LOG_LEVEL        = "INFO"
      MERGEGUARD_DRY_RUN          = "false"
      MERGEGUARD_COMMIT_STATUS    = "true"
      MERGEGUARD_SCAN_ALLOW_PATHS = ""
    })
    error_message = "Worker environment differs from ARCHITECTURE.md section 10."
  }

  # Queue, dead-letter queue and the mapping between queue and worker.
  assert {
    condition     = aws_sqs_queue.review_requests.name == "mergeguard-review-requests" && aws_sqs_queue.dlq.name == "mergeguard-review-requests-dlq"
    error_message = "Queue names differ from the docs."
  }
  assert {
    condition     = aws_sqs_queue.review_requests.sqs_managed_sse_enabled && aws_sqs_queue.dlq.sqs_managed_sse_enabled
    error_message = "Both queues must be encrypted at rest."
  }
  assert {
    condition     = jsondecode(aws_sqs_queue.review_requests.redrive_policy).maxReceiveCount == 3
    error_message = "Requests must move to the dead-letter queue after three receives (ADR-0005)."
  }
  assert {
    condition     = aws_sqs_queue.review_requests.visibility_timeout_seconds == 360
    error_message = "The visibility timeout must be six times the worker timeout."
  }
  assert {
    condition     = aws_sqs_queue.review_requests.message_retention_seconds == 86400 && aws_sqs_queue.dlq.message_retention_seconds == 1209600
    error_message = "Retention must be one day for the queue and 14 days for the dead-letter queue."
  }
  assert {
    condition     = aws_lambda_event_source_mapping.worker.batch_size == 1 && aws_lambda_event_source_mapping.worker.function_response_types == toset(["ReportBatchItemFailures"])
    error_message = "The worker takes one request per invocation and reports item failures."
  }
  assert {
    condition     = aws_lambda_event_source_mapping.worker.scaling_config[0].maximum_concurrency == 2
    error_message = "Worker concurrency must default to 2."
  }

  # Logs.
  assert {
    condition     = aws_cloudwatch_log_group.lambda.name == "/aws/lambda/mergeguard-ai-pr-reviewer" && aws_cloudwatch_log_group.lambda.retention_in_days == 14
    error_message = "The receiver log group must exist with 14-day retention."
  }
  assert {
    condition     = aws_cloudwatch_log_group.worker.name == "/aws/lambda/mergeguard-ai-pr-worker" && aws_cloudwatch_log_group.worker.retention_in_days == 14
    error_message = "The worker log group must exist with 14-day retention."
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
    error_message = "Integration must use payload 2.0 and the receiver timeout."
  }
  assert {
    condition     = aws_lambda_permission.api.principal == "apigateway.amazonaws.com"
    error_message = "Only API Gateway may invoke the receiver."
  }

  # Alarms (T-506): dead-letter queue, 5xx, and Lambda errors for both functions.
  assert {
    condition     = aws_cloudwatch_metric_alarm.dlq_not_empty.metric_name == "ApproximateNumberOfMessagesVisible" && aws_cloudwatch_metric_alarm.dlq_not_empty.threshold == 0 && aws_cloudwatch_metric_alarm.dlq_not_empty.comparison_operator == "GreaterThanThreshold"
    error_message = "The DLQ alarm must fire as soon as the dead-letter queue holds a message."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.dlq_not_empty.dimensions == tomap({ QueueName = "mergeguard-review-requests-dlq" })
    error_message = "The DLQ alarm must watch the dead-letter queue."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.webhook_5xx.metric_name == "5xx" && aws_cloudwatch_metric_alarm.webhook_5xx.namespace == "AWS/ApiGateway" && aws_cloudwatch_metric_alarm.webhook_5xx.period == 900
    error_message = "The 5xx alarm must watch API Gateway over 15 minutes."
  }
  assert {
    condition     = toset(keys(aws_cloudwatch_metric_alarm.lambda_errors)) == toset(["receiver", "worker"])
    error_message = "Both Lambda functions need an Errors alarm."
  }
  assert {
    condition     = alltrue([for alarm in aws_cloudwatch_metric_alarm.lambda_errors : alarm.metric_name == "Errors" && alarm.period == 900 && alarm.threshold == 0])
    error_message = "Lambda Errors alarms must fire on any error within 15 minutes."
  }
  assert {
    condition = alltrue([
      for alarm in [
        aws_cloudwatch_metric_alarm.dlq_not_empty,
        aws_cloudwatch_metric_alarm.webhook_5xx,
        aws_cloudwatch_metric_alarm.lambda_errors["receiver"],
        aws_cloudwatch_metric_alarm.lambda_errors["worker"],
      ] : alarm.treat_missing_data == "notBreaching" && length(alarm.alarm_actions) == 1
    ])
    error_message = "Every alarm must notify the alerts topic and treat missing data as healthy."
  }

  # Budget and subscription: nothing is created until the owner supplies values.
  assert {
    condition     = length(aws_budgets_budget.monthly) == 0 && length(aws_sns_topic_subscription.email) == 0
    error_message = "No budget or email subscription without monthly_budget_usd and alert_email."
  }
}

run "custom_prefix_moves_parameters_and_iam_scope" {
  command = plan

  variables {
    param_prefix = "/team/mergeguard"
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.lambda.policy).Statement[1].Resource == "arn:aws:ssm:us-east-1:123456789012:parameter/team/mergeguard/webhook_secret"
    error_message = "The receiver's SSM scope must follow param_prefix."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.worker.policy).Statement[1].Resource == ["arn:aws:ssm:us-east-1:123456789012:parameter/team/mergeguard/github_token", "arn:aws:ssm:us-east-1:123456789012:parameter/team/mergeguard/gemini_api_key"]
    error_message = "The worker's SSM scope must follow param_prefix."
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
    error_message = "reserved_concurrency must reach the receiver."
  }
}

run "worker_settings_reach_the_worker_and_the_queue" {
  command = plan

  variables {
    worker_timeout_seconds  = 120
    worker_max_concurrency  = 5
    queue_max_receive_count = 5
    commit_status_enabled   = false
    dry_run                 = true
    scan_allow_paths        = ["tests/fixtures/**", "**/*.pem"]
  }

  assert {
    condition     = aws_lambda_function.worker.timeout == 120 && aws_sqs_queue.review_requests.visibility_timeout_seconds == 720
    error_message = "The visibility timeout must follow the worker timeout (6x)."
  }
  assert {
    condition     = aws_lambda_event_source_mapping.worker.scaling_config[0].maximum_concurrency == 5
    error_message = "worker_max_concurrency must reach the event source mapping."
  }
  assert {
    condition     = jsondecode(aws_sqs_queue.review_requests.redrive_policy).maxReceiveCount == 5
    error_message = "queue_max_receive_count must reach the redrive policy."
  }
  assert {
    condition     = aws_lambda_function.worker.environment[0].variables["MERGEGUARD_COMMIT_STATUS"] == "false" && aws_lambda_function.worker.environment[0].variables["MERGEGUARD_DRY_RUN"] == "true"
    error_message = "commit_status_enabled and dry_run must reach the worker."
  }
  assert {
    condition     = aws_lambda_function.worker.environment[0].variables["MERGEGUARD_SCAN_ALLOW_PATHS"] == "tests/fixtures/**,**/*.pem"
    error_message = "scan_allow_paths must be passed to the worker as a comma-separated list."
  }
  assert {
    condition     = !contains(keys(aws_lambda_function.reviewer.environment[0].variables), "MERGEGUARD_SCAN_ALLOW_PATHS")
    error_message = "Review settings belong to the worker, not the receiver."
  }
}

run "alert_email_subscribes_and_a_budget_is_created" {
  command = plan

  variables {
    alert_email        = "alerts@example.com"
    monthly_budget_usd = 15
  }

  assert {
    condition     = length(aws_sns_topic_subscription.email) == 1 && aws_sns_topic_subscription.email[0].protocol == "email" && aws_sns_topic_subscription.email[0].endpoint == "alerts@example.com"
    error_message = "alert_email must subscribe to the alerts topic."
  }
  assert {
    condition     = length(aws_budgets_budget.monthly) == 1 && aws_budgets_budget.monthly[0].limit_amount == "15" && aws_budgets_budget.monthly[0].limit_unit == "USD" && aws_budgets_budget.monthly[0].time_unit == "MONTHLY"
    error_message = "The budget must be a monthly USD cost budget."
  }
  assert {
    condition = toset([for n in aws_budgets_budget.monthly[0].notification : "${n.notification_type}:${n.threshold}"]) == toset([
      "ACTUAL:80", "FORECASTED:100",
    ])
    error_message = "The budget must alert at 80% of actual and 100% of forecast cost."
  }
  assert {
    condition     = alltrue([for n in aws_budgets_budget.monthly[0].notification : n.subscriber_email_addresses == toset(["alerts@example.com"])])
    error_message = "Budget notifications must go to alert_email."
  }
}

run "a_budget_without_an_email_is_refused" {
  command = plan

  variables {
    monthly_budget_usd = 15
  }

  expect_failures = [aws_budgets_budget.monthly]
}

run "rejects_timeout_above_api_gateway_limit" {
  command = plan

  variables {
    lambda_timeout_seconds = 31
  }

  expect_failures = [var.lambda_timeout_seconds]
}

run "rejects_a_scan_allow_list_that_switches_the_scanner_off" {
  command = plan

  variables {
    scan_allow_paths = ["**"]
  }

  expect_failures = [var.scan_allow_paths]
}

run "rejects_an_absolute_scan_allow_path" {
  command = plan

  variables {
    scan_allow_paths = ["/etc/**"]
  }

  expect_failures = [var.scan_allow_paths]
}

run "rejects_a_worker_concurrency_below_the_lambda_minimum" {
  command = plan

  variables {
    worker_max_concurrency = 1
  }

  expect_failures = [var.worker_max_concurrency]
}

run "rejects_a_zero_receive_count" {
  command = plan

  variables {
    queue_max_receive_count = 0
  }

  expect_failures = [var.queue_max_receive_count]
}

run "rejects_a_malformed_alert_email" {
  command = plan

  variables {
    alert_email = "not-an-email"
  }

  expect_failures = [var.alert_email]
}
