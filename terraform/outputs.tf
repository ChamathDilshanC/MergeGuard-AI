output "github_webhook_url" {
  description = "Payload URL for the GitHub webhook (DEPLOYMENT.md section 6)."
  value       = "${aws_apigatewayv2_api.webhook.api_endpoint}/webhook"
}

output "ssm_parameter_names" {
  description = "Set the real values with aws ssm put-parameter --overwrite before connecting the webhook (DEPLOYMENT.md section 3)."
  value       = sort([for parameter in aws_ssm_parameter.secret : parameter.name])
}

output "lambda_function_name" {
  description = "Name of the reviewer Lambda function."
  value       = aws_lambda_function.reviewer.function_name
}

output "lambda_log_group" {
  description = "CloudWatch log group with the Lambda's JSON logs."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "api_access_log_group" {
  description = "CloudWatch log group with API Gateway access logs."
  value       = aws_cloudwatch_log_group.api_access.name
}

output "review_queue_url" {
  description = "SQS queue that holds review requests (metadata only)."
  value       = aws_sqs_queue.review_requests.url
}

output "review_dlq_url" {
  description = "Dead-letter queue for review requests that failed repeatedly. An alarm fires when it is not empty (docs/DEPLOYMENT.md section 10)."
  value       = aws_sqs_queue.dlq.url
}

output "worker_function_name" {
  description = "Name of the SQS worker Lambda function."
  value       = aws_lambda_function.worker.function_name
}

output "worker_log_group" {
  description = "CloudWatch log group with the worker's JSON logs."
  value       = aws_cloudwatch_log_group.worker.name
}

output "alerts_topic_arn" {
  description = "SNS topic that receives alarm notifications."
  value       = aws_sns_topic.alerts.arn
}
