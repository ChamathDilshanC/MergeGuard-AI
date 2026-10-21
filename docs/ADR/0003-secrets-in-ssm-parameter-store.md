# ADR-0003 — Secrets in SSM Parameter Store

- **Status:** Accepted (brief). The second decision bullet is amended by [ADR-0007](0007-ssm-placeholders-with-write-only-values.md): Terraform now manages the parameters with write-only placeholders, and values still never enter state
- **Date:** 2026-10-21
- **Related:** [SECURITY.md](../SECURITY.md) §5.4, §6 · PRD FR-011

## Context

MergeGuard needs three secrets at runtime: a GitHub token, the webhook secret, and a Gemini API key. They must not appear in source code, Terraform files or state, or Lambda environment variables (which are visible to anyone with `lambda:GetFunctionConfiguration`).

## Decision

- Store the secrets as **SSM Parameter Store `SecureString`** parameters under `/mergeguard/`: `github_token`, `webhook_secret`, `gemini_api_key`. They are encrypted with the AWS-managed key `aws/ssm`.
- Create them **outside Terraform** with the AWS CLI, so their values never enter Terraform state.
- The Lambda reads them at runtime with one `GetParameters` call (`WithDecryption=true`) and caches them per execution environment with a TTL (Proposed refinement of the brief's three import-time calls).

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| AWS Secrets Manager | Built-in rotation and cross-account sharing, but a per-secret monthly cost and features MergeGuard does not need yet |
| Lambda environment variables | Readable through the Lambda API and console; easy to leak |
| Terraform-managed SSM parameters | Values would be stored in Terraform state |

## Consequences

- Low cost and simple IAM (`ssm:GetParameter(s)` on one path).
- Rotation is manual ([SECURITY.md](../SECURITY.md) §6). The cache TTL means rotated values take effect without a redeploy.
- The operator must create the parameters before the first invocation; the Lambda fails closed until then.
- Using a customer-managed KMS key later requires adding `kms:Decrypt` on that key.
