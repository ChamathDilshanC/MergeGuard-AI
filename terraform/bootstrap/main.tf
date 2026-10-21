# One-time bootstrap for CI deployment (T-505): the encrypted, versioned S3 bucket
# that holds the main stack's Terraform state, and the IAM role that the deploy
# workflow assumes through GitHub's OIDC provider (no long-lived AWS keys in
# GitHub). The maintainer applies this stack by hand, once, with local state;
# see docs/DEPLOYMENT.md section 8. Nothing here is applied by CI.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  # The ARN of the GitHub OIDC provider is fixed, so the role's trust policy does
  # not depend on whether this stack created the provider.
  oidc_url          = "token.actions.githubusercontent.com"
  oidc_provider_arn = "arn:aws:iam::${local.account_id}:oidc-provider/${local.oidc_url}"

  bucket_arn   = "arn:aws:s3:::${var.state_bucket_name}"
  state_prefix = dirname(var.state_key)

  # The main stack names everything mergeguard* (docs/DESIGN_SYSTEM.md section 8),
  # which is what scopes the deploy role.
  arn_lambda = "arn:aws:lambda:${local.region}:${local.account_id}:function:mergeguard-*"
  arn_roles  = "arn:aws:iam::${local.account_id}:role/mergeguard_*"
  arn_sqs    = "arn:aws:sqs:${local.region}:${local.account_id}:mergeguard-*"
  arn_sns    = "arn:aws:sns:${local.region}:${local.account_id}:mergeguard-*"
  arn_alarms = "arn:aws:cloudwatch:${local.region}:${local.account_id}:alarm:mergeguard-*"
  arn_budget = "arn:aws:budgets::${local.account_id}:budget/mergeguard-*"
  arn_params = "arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.param_prefix}/*"
  arn_apis = [
    "arn:aws:apigateway:${local.region}::/apis",
    "arn:aws:apigateway:${local.region}::/apis/*",
    "arn:aws:apigateway:${local.region}::/tags/*",
  ]
  arn_log_groups = [
    "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/mergeguard-*",
    "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/mergeguard-*:*",
    "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/apigateway/mergeguard-*",
    "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/apigateway/mergeguard-*:*",
  ]

  in_region = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
}

# --- Remote state bucket ----------------------------------------------------------

resource "aws_s3_bucket" "state" {
  #checkov:skip=CKV_AWS_18:Access logging needs a second bucket to hold the logs; CloudTrail already records access to this bucket.
  #checkov:skip=CKV_AWS_144:Cross-region replication is not needed for state that can be rebuilt from the stack; versioning protects against bad writes.
  #checkov:skip=CKV_AWS_145:State holds no secret values by design (ADR-0007), so SSE-S3 is enough; a customer-managed key adds cost and a key policy.
  #checkov:skip=CKV2_AWS_62:No consumer needs event notifications for the state bucket.
  bucket = var.state_bucket_name

  # Losing state would orphan the stack, so never let `terraform destroy` or an
  # accidental plan delete this bucket.
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.state]
}

# --- GitHub OIDC provider and the deploy role -------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://${local.oidc_url}"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "deploy" {
  name                 = "mergeguard-github-deploy"
  description          = "Assumed by the MergeGuard deploy workflow through GitHub OIDC. No long-lived keys."
  max_session_duration = 3600

  # Only this repository's plan and apply environments can assume the role. The
  # apply environment requires approval, so GitHub withholds its token (and AWS
  # access) until a reviewer approves the run.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Federated = local.oidc_provider_arn }
        Action    = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${local.oidc_url}:aud" = "sts.amazonaws.com"
            "${local.oidc_url}:sub" = [
              "repo:${var.github_repository}:environment:${var.plan_environment}",
              "repo:${var.github_repository}:environment:${var.apply_environment}",
            ]
          }
        }
      },
    ]
  })

  depends_on = [aws_iam_openid_connect_provider.github]
}

# A starting point derived from the resources in terraform/main.tf and alerts.tf,
# scoped by the mergeguard* naming, the account and the region. The first real
# deploy (T-402) may find an action missing; add it here, never widen to "*".
resource "aws_iam_role_policy" "deploy" {
  #checkov:skip=CKV_AWS_355:The few actions that AWS defines without resource-level permissions (describe and list calls, log delivery for API access logs) need Resource "*"; every write action is scoped to mergeguard* ARNs.
  #checkov:skip=CKV_AWS_290:Write access is limited to mergeguard* resources in this account and region; see SECURITY.md 5.10 for the residual risk of a deploy role that manages IAM roles.
  #checkov:skip=CKV_AWS_287:The role cannot read credentials; SSM parameters are the only secrets store it touches, and ADR-0007 keeps real values out of state.
  #checkov:skip=CKV_AWS_289:The deploy role must create the stack's own IAM roles; the actions are limited to role/mergeguard_* and PassRole to Lambda only (SECURITY.md 5.10).
  #checkov:skip=CKV_AWS_288:The role may read SSM parameters under its own prefix because the AWS provider reads a parameter on refresh; see SECURITY.md 5.10.
  name = "mergeguard_deploy_policy"
  role = aws_iam_role.deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "StateBucketList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = local.bucket_arn
      },
      {
        Sid      = "StateObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${local.bucket_arn}/${local.state_prefix}/*"
      },
      {
        Sid    = "ManageOwnRoles"
        Effect = "Allow"
        Action = [
          "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole",
          "iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy", "iam:GetRolePolicy",
          "iam:DeleteRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
          "iam:ListInstanceProfilesForRole", "iam:TagRole", "iam:UntagRole",
        ]
        Resource = local.arn_roles
      },
      {
        Sid       = "PassOwnRolesToLambda"
        Effect    = "Allow"
        Action    = ["iam:PassRole"]
        Resource  = local.arn_roles
        Condition = { StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" } }
      },
      {
        Sid    = "ManageFunctions"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:GetFunction",
          "lambda:GetFunctionConfiguration", "lambda:GetFunctionCodeSigningConfig",
          "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
          "lambda:ListVersionsByFunction", "lambda:AddPermission", "lambda:RemovePermission",
          "lambda:GetPolicy", "lambda:PutFunctionConcurrency", "lambda:DeleteFunctionConcurrency",
          "lambda:TagResource", "lambda:UntagResource", "lambda:ListTags",
        ]
        Resource  = local.arn_lambda
        Condition = local.in_region
      },
      {
        Sid    = "ManageEventSourceMappingsOfOwnFunctions"
        Effect = "Allow"
        Action = [
          "lambda:CreateEventSourceMapping", "lambda:UpdateEventSourceMapping",
          "lambda:DeleteEventSourceMapping",
        ]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = var.aws_region }
          ArnLike      = { "lambda:FunctionArn" = local.arn_lambda }
        }
      },
      {
        Sid      = "ManageApi"
        Effect   = "Allow"
        Action   = ["apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE"]
        Resource = local.arn_apis
      },
      {
        Sid    = "ManageLogGroups"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy",
          "logs:DeleteRetentionPolicy", "logs:TagResource", "logs:UntagResource",
          "logs:ListTagsForResource", "logs:ListTagsLogGroup", "logs:TagLogGroup",
          "logs:UntagLogGroup",
        ]
        Resource  = local.arn_log_groups
        Condition = local.in_region
      },
      {
        Sid       = "ManageParameters"
        Effect    = "Allow"
        Action    = ["ssm:PutParameter", "ssm:DeleteParameter", "ssm:GetParameter", "ssm:GetParameters", "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource", "ssm:ListTagsForResource"]
        Resource  = local.arn_params
        Condition = local.in_region
      },
      {
        Sid    = "ManageQueues"
        Effect = "Allow"
        Action = [
          "sqs:CreateQueue", "sqs:DeleteQueue", "sqs:GetQueueAttributes", "sqs:SetQueueAttributes",
          "sqs:GetQueueUrl", "sqs:TagQueue", "sqs:UntagQueue", "sqs:ListQueueTags",
        ]
        Resource  = local.arn_sqs
        Condition = local.in_region
      },
      {
        Sid    = "ManageTopics"
        Effect = "Allow"
        Action = [
          "sns:CreateTopic", "sns:DeleteTopic", "sns:GetTopicAttributes", "sns:SetTopicAttributes",
          "sns:TagResource", "sns:UntagResource", "sns:ListTagsForResource", "sns:Subscribe",
          "sns:Unsubscribe", "sns:GetSubscriptionAttributes", "sns:SetSubscriptionAttributes",
          "sns:ListSubscriptionsByTopic",
        ]
        Resource  = local.arn_sns
        Condition = local.in_region
      },
      {
        Sid       = "ManageAlarms"
        Effect    = "Allow"
        Action    = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:TagResource", "cloudwatch:UntagResource", "cloudwatch:ListTagsForResource"]
        Resource  = local.arn_alarms
        Condition = local.in_region
      },
      {
        Sid      = "ManageBudgets"
        Effect   = "Allow"
        Action   = ["budgets:ViewBudget", "budgets:ModifyBudget", "budgets:TagResource", "budgets:UntagResource", "budgets:ListTagsForResource"]
        Resource = local.arn_budget
      },
      {
        # Describe and list calls, event source mapping reads, and the log delivery
        # actions API Gateway needs for access logs. AWS defines none of these with
        # resource-level permissions.
        Sid    = "ActionsWithoutResourceLevelPermissions"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups", "logs:CreateLogDelivery", "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery", "logs:GetLogDelivery", "logs:ListLogDeliveries",
          "logs:PutResourcePolicy", "logs:DescribeResourcePolicies",
          "ssm:DescribeParameters", "cloudwatch:DescribeAlarms", "sqs:ListQueues",
          "lambda:GetEventSourceMapping", "lambda:ListEventSourceMappings",
        ]
        Resource  = "*"
        Condition = local.in_region
      },
    ]
  })
}
