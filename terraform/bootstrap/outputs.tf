output "state_bucket_name" {
  description = "Set as the GitHub repository variable TF_STATE_BUCKET (docs/DEPLOYMENT.md section 8)."
  value       = aws_s3_bucket.state.bucket
}

output "deploy_role_arn" {
  description = "Set as the GitHub repository variable AWS_DEPLOY_ROLE_ARN. It is not a secret."
  value       = aws_iam_role.deploy.arn
}

output "state_key" {
  description = "Object key the deploy workflow uses for the main stack's state."
  value       = var.state_key
}
