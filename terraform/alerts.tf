# Alarms and the cost budget (T-506, NFR-007, SECURITY.md TM-08).
#
# Alarms always exist and publish to one SNS topic. With alert_email set, that
# address is subscribed (confirm the subscription from the email AWS sends).
# Without it the alarms still show in the CloudWatch console.

resource "aws_sns_topic" "alerts" {
  #checkov:skip=CKV_AWS_26:CloudWatch alarms cannot publish to a topic encrypted with the AWS-managed key, and a customer-managed key adds cost and a key policy. Messages carry alarm names and metric values only (SECURITY.md 5.9).
  name = "mergeguard-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email == null ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Failed work ends here once SQS has retried it queue_max_receive_count times
# (ADR-0005). The worker reports a failed request instead of crashing, so Lambda
# Errors would never show it; this alarm does.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "mergeguard-review-dlq-not-empty"
  alarm_description   = "A review request failed ${var.queue_max_receive_count} times and is in the dead-letter queue. Fix the cause (docs/DEPLOYMENT.md section 10), then redrive."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# The receiver turns every failure into an HTTP 500, so API Gateway's 5xx metric
# is the signal that deliveries are failing (bad secret, SQS or SSM trouble).
resource "aws_cloudwatch_metric_alarm" "webhook_5xx" {
  alarm_name          = "mergeguard-webhook-5xx"
  alarm_description   = "The webhook endpoint returned 5xx. GitHub does not retry; redeliver after fixing the cause (docs/DEPLOYMENT.md section 10)."
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = aws_apigatewayv2_api.webhook.id }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# Lambda Errors still catch what the handlers cannot: timeouts, out-of-memory
# kills and import-time crashes (compare RB-01).
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = {
    receiver = aws_lambda_function.reviewer.function_name
    worker   = aws_lambda_function.worker.function_name
  }

  alarm_name          = "mergeguard-${each.key}-lambda-errors"
  alarm_description   = "The ${each.key} Lambda failed outright (timeout, crash or out of memory). Check ${each.value} in CloudWatch Logs."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# Account-wide monthly cost. The amount is the owner's decision (OQ-05), so no
# budget exists until monthly_budget_usd is set.
resource "aws_budgets_budget" "monthly" {
  count = var.monthly_budget_usd == null ? 0 : 1

  name         = "mergeguard-monthly-cost"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }

  lifecycle {
    precondition {
      condition     = var.alert_email != null
      error_message = "monthly_budget_usd needs alert_email: a budget with nobody to notify is no alert."
    }
  }
}
