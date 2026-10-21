# Credential-free plan checks for the bootstrap stack (T-505). The AWS provider is
# mocked, so no AWS account, credentials or API calls are involved. Run with:
#   terraform -chdir=terraform/bootstrap test

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_region" {
    defaults = { name = "us-east-1" }
  }
}

variables {
  state_bucket_name = "example-mergeguard-tfstate"
}

run "state_bucket_is_private_versioned_and_encrypted" {
  command = plan

  assert {
    condition     = aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled"
    error_message = "State versioning must be enabled."
  }
  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256"
    error_message = "State must be encrypted at rest."
  }
  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.state.block_public_acls,
      aws_s3_bucket_public_access_block.state.block_public_policy,
      aws_s3_bucket_public_access_block.state.ignore_public_acls,
      aws_s3_bucket_public_access_block.state.restrict_public_buckets,
    ])
    error_message = "All four public access blocks must be on."
  }
  assert {
    condition     = aws_s3_bucket.state.force_destroy == false
    error_message = "The state bucket must never be force-destroyed."
  }
  assert {
    condition = (
      jsondecode(aws_s3_bucket_policy.state.policy).Statement[0].Effect == "Deny"
      && jsondecode(aws_s3_bucket_policy.state.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    )
    error_message = "The bucket must refuse requests that do not use TLS."
  }
  assert {
    condition     = aws_s3_bucket_ownership_controls.state.rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "ACLs must be disabled."
  }
}

run "only_this_repositorys_environments_can_assume_the_deploy_role" {
  command = plan

  assert {
    condition     = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Principal.Federated == "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    error_message = "Only the GitHub OIDC provider may federate into the role."
  }
  assert {
    condition     = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Action == "sts:AssumeRoleWithWebIdentity"
    error_message = "The role must be assumed with a web identity token, not with keys."
  }
  assert {
    condition     = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "The audience must be sts.amazonaws.com."
  }
  assert {
    condition = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == [
      "repo:ChamathDilshanC/MergeGuard-AI:environment:production-plan",
      "repo:ChamathDilshanC/MergeGuard-AI:environment:production",
    ]
    error_message = "Only the plan and apply environments of this repository may assume the role."
  }
  assert {
    condition     = !strcontains(aws_iam_role.deploy.assume_role_policy, "*")
    error_message = "The trust policy must contain no wildcard."
  }
  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 1 && aws_iam_openid_connect_provider.github[0].client_id_list == toset(["sts.amazonaws.com"])
    error_message = "The GitHub OIDC provider must be created for sts.amazonaws.com by default."
  }
  assert {
    condition     = aws_iam_role.deploy.max_session_duration == 3600
    error_message = "Deploy sessions must stay short."
  }
}

run "deploy_policy_is_scoped_to_the_stack" {
  command = plan

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      !anytrue([for action in statement.Action : strcontains(action, "*")])
    ])
    error_message = "No wildcard actions are allowed."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      !anytrue([for action in statement.Action : startswith(action, "iam:") && contains(["iam:AttachRolePolicy", "iam:CreateUser", "iam:CreateAccessKey", "iam:PutUserPolicy", "iam:CreatePolicy", "iam:CreatePolicyVersion"], action)])
    ])
    error_message = "The deploy role must not manage users, access keys or managed policies."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      statement.Resource != "*" || statement.Sid == "ActionsWithoutResourceLevelPermissions" || statement.Sid == "ManageEventSourceMappingsOfOwnFunctions"
    ])
    error_message = "Only the two documented statements may use Resource \"*\"."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      statement.Sid != "ManageOwnRoles" || statement.Resource == "arn:aws:iam::123456789012:role/mergeguard_*"
    ])
    error_message = "IAM role management must be limited to role/mergeguard_*."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      statement.Sid != "PassOwnRolesToLambda" || statement.Condition.StringEquals["iam:PassedToService"] == "lambda.amazonaws.com"
    ])
    error_message = "PassRole must be limited to Lambda."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      statement.Sid != "StateObjects" || statement.Resource == "arn:aws:s3:::example-mergeguard-tfstate/mergeguard/*"
    ])
    error_message = "State access must be limited to the stack's key prefix."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      statement.Sid != "ManageParameters" || statement.Resource == "arn:aws:ssm:us-east-1:123456789012:parameter/mergeguard/*"
    ])
    error_message = "SSM access must be limited to the MergeGuard prefix."
  }
  assert {
    # Global services and ARNs that already name the region are exempt.
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      contains(["StateBucketList", "StateObjects", "ManageOwnRoles", "PassOwnRolesToLambda", "ManageApi", "ManageBudgets"], statement.Sid)
      || try(statement.Condition.StringEquals["aws:RequestedRegion"], "") == "us-east-1"
    ])
    error_message = "Regional statements must be limited to the deployment region."
  }
}

run "an_existing_oidc_provider_is_not_recreated" {
  command = plan

  variables {
    create_oidc_provider = false
  }

  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 0
    error_message = "No provider must be created when one already exists."
  }
  assert {
    condition     = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Principal.Federated == "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    error_message = "The trust policy must still point at the existing provider."
  }
}

run "environments_and_repository_can_be_changed" {
  command = plan

  variables {
    github_repository = "someone/else"
    apply_environment = "prod"
    state_key         = "team/state.tfstate"
  }

  assert {
    condition = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == [
      "repo:someone/else:environment:production-plan",
      "repo:someone/else:environment:prod",
    ]
    error_message = "The trust policy must follow the repository and environment variables."
  }
}

run "rejects_an_invalid_bucket_name" {
  command = plan

  variables {
    state_bucket_name = "Not_A_Bucket"
  }

  expect_failures = [var.state_bucket_name]
}

run "rejects_an_invalid_repository" {
  command = plan

  variables {
    github_repository = "*"
  }

  expect_failures = [var.github_repository]
}
