# MergeGuard AI — Deployment and Operations

| Field | Value |
| --- | --- |
| Status | Draft v0.1 — **procedure not yet executed**. The code (M2–M3) and the Terraform stack (T-401) exist and are tested without AWS; the first real deploy is T-402 |
| Last updated | 2026-10-21 |
| Related | [Architecture](ARCHITECTURE.md) §14 · [Security](SECURITY.md) §6 · [Testing](TESTING_STRATEGY.md) §7 |

This guide turns the brief's four deployment steps into a repeatable procedure with safer defaults. Commands are written for a POSIX shell; PowerShell equivalents are given where they differ.

## 1. Environments

| Environment | Purpose | Phase |
| --- | --- | --- |
| `sandbox` | One AWS account and region (default `us-east-1`), webhook on a sandbox repository | 1 |
| `production` | Webhook on real repositories; deployed from CI with approval | 2 (Proposed) |

In Phase 1 the sandbox stack may also serve the maintainer's real repositories, once the MVP exit criteria (PRD §8) pass.

## 2. Prerequisites

| Requirement | Notes |
| --- | --- |
| AWS account and IAM principal | Permissions to manage IAM roles and policies, Lambda, API Gateway v2, CloudWatch Logs, and SSM parameters. Check with `aws sts get-caller-identity` |
| AWS CLI | v2 recommended. Credentials configured for the target account |
| Terraform | `>= 1.11, < 2.0` (`terraform/versions.tf`); 1.11 adds the write-only arguments that keep secrets out of state (ADR-0007). CI uses 1.16.5 |
| GitHub | Admin rights on each target repository (to add a webhook) |
| Google AI Studio account | To create a Gemini API key; tier per [OQ-05](PRD.md#17-open-questions) |
| Python 3.12 | Only for development and tests |

**Cost note:** at small scale, the design keeps AWS usage within low-cost service tiers. Free-tier eligibility depends on the account's plan and age, so check the AWS Free Tier page for your account. Gemini pricing and quotas are separate.

## 3. Secrets: placeholders, then real values

**Order of operations:**

1. Prepare the credentials (§3.1).
2. Deploy (§5). Terraform creates the three SSM parameters with a public placeholder value ([ADR-0007](ADR/0007-ssm-placeholders-with-write-only-values.md)).
3. Store the real values (§3.2).
4. Connect the webhook (§6).

Until step 3, the Lambda treats each parameter as not set: every delivery gets `500` (`config.failed`), and nothing leaves AWS.

### 3.1 Prepare the credentials

1. **GitHub fine-grained token.** Go to GitHub → *Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token* and set:
   - **Expiration:** set one (record the date for rotation).
   - **Repository access:** *Only select repositories*.
   - **Permissions:** *Pull requests: Read and write*, *Contents: Read-only*. *Metadata: Read* is added automatically. From Phase 2, also *Commit statuses: Read and write*.
   - Prefer a dedicated machine account ([OQ-06](PRD.md#17-open-questions)). If the PR author owns the token, GitHub refuses `REQUEST_CHANGES` and MergeGuard falls back to a comment (FR-010).
2. **Gemini API key.** Go to Google AI Studio → *Get API key* and create a key.
3. **Webhook secret.** Generate a random value. Do not invent a memorable one.

   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

### 3.2 Store the real values (after the first apply)

The parameters already exist, so always use `--overwrite`. Read each value interactively so it never appears in shell history:

```bash
REGION=us-east-1
read -rsp "GitHub token: " V && echo
aws ssm put-parameter --name "/mergeguard/github_token" --type SecureString --overwrite --value "$V" --region "$REGION"
read -rsp "Gemini API key: " V && echo
aws ssm put-parameter --name "/mergeguard/gemini_api_key" --type SecureString --overwrite --value "$V" --region "$REGION"
read -rsp "Webhook secret: " V && echo
aws ssm put-parameter --name "/mergeguard/webhook_secret" --type SecureString --overwrite --value "$V" --region "$REGION"
unset V
```

PowerShell 7.1+:

```powershell
$v = Read-Host "GitHub token" -MaskInput
aws ssm put-parameter --name "/mergeguard/github_token" --type SecureString --overwrite --value $v --region us-east-1
# repeat for /mergeguard/gemini_api_key and /mergeguard/webhook_secret
Remove-Variable v
```

**Never** print values back with `get-parameter --with-decryption` in shared terminals, CI logs, or screen recordings.

**Never run `terraform import` on these parameters.** An import reads the real value into Terraform state and its backups (ADR-0007). Terraform writes only the placeholder and never reads the real value back.

### 3.3 The Gemini key and OQ-05

You can leave `/mergeguard/gemini_api_key` as the placeholder. MergeGuard then still runs the secret scan, posts *AI review unavailable (not_configured)* on clean PRs, and **sends no diff to Gemini**.

Store the real key only after the Gemini tier is decided ([OQ-05](PRD.md#17-open-questions)), because from then on live PR diffs go to Google.

## 4. Terraform configuration

These variables live in `terraform/variables.tf` and are used by `terraform/main.tf` (T-401), with validation for the limits stated below. Defaults reproduce the brief.

| Variable | Default | Purpose |
| --- | --- | --- |
| `aws_region` | `us-east-1` | Deployment region |
| `function_name` | `mergeguard-ai-pr-reviewer` | Lambda name; also sets the log group name |
| `lambda_timeout_seconds` | `30` | Must stay ≤ 30 while API Gateway invokes synchronously |
| `lambda_memory_mb` | `256` | From the brief |
| `log_retention_days` | `14` | CloudWatch retention |
| `param_prefix` | `/mergeguard` | SSM prefix → `MERGEGUARD_PARAM_PREFIX` |
| `gemini_model` | `gemini-2.5-flash` | → `MERGEGUARD_GEMINI_MODEL` |
| `max_diff_chars` | `12000` | → `MERGEGUARD_MAX_DIFF_CHARS` |
| `max_files` | `300` | → `MERGEGUARD_MAX_FILES` |
| `log_level` | `INFO` | → `MERGEGUARD_LOG_LEVEL` |
| `dry_run` | `false` | → `MERGEGUARD_DRY_RUN` |
| `throttling_rate_limit` / `throttling_burst_limit` | `10` / `20` | API Gateway stage throttling |
| `reserved_concurrency` | unset (`-1`, unreserved) | Optional cap. Set it once the account's concurrency quota allows ([SECURITY.md](SECURITY.md) §5.9) |

Put local overrides in `terraform/terraform.tfvars`, which must be git-ignored. It must never contain secrets.

## 5. Deploy

```bash
terraform -chdir=terraform init
terraform -chdir=terraform test          # no credentials needed; mocked AWS provider
terraform -chdir=terraform plan -out=tfplan
# Review the plan: 13 resources to add (table below)
terraform -chdir=terraform apply tfplan
terraform -chdir=terraform output -raw github_webhook_url
```

The brief used `terraform apply -auto-approve`. Reviewing a saved plan is the default here, because the plan shows exactly which IAM permissions are about to be granted.

**What the plan creates** (verified without credentials by `terraform test`):

| Resource | Purpose |
| --- | --- |
| `aws_ssm_parameter.secret` (3) | `github_token`, `webhook_secret`, `gemini_api_key` as `SecureString` placeholders (§3) |
| `aws_iam_role.lambda`, `aws_iam_role_policy.lambda` | Execution role with exactly the [SECURITY.md](SECURITY.md) §5.3 policy |
| `aws_cloudwatch_log_group.lambda`, `.api_access` | Lambda logs and API Gateway access logs, 14-day retention |
| `aws_lambda_function.reviewer` | Python 3.12, 256 MB, 30 s, code zipped from `src/` |
| `aws_apigatewayv2_api.webhook`, `_stage.default`, `_integration.lambda`, `_route.webhook` | HTTP API, `$default` stage with throttling and access logs, `POST /webhook` only |
| `aws_lambda_permission.api` | Lets only `POST /webhook` of this API invoke the function |

## 6. Connect the GitHub webhook

In each target repository, go to *Settings → Webhooks → Add webhook* and set:

| Field | Value |
| --- | --- |
| Payload URL | The `github_webhook_url` output |
| Content type | `application/json` (required) |
| Secret | The webhook secret from §3 |
| SSL verification | Enabled |
| Events | *Let me select individual events* → **Pull requests** only; untick *Pushes* |
| Active | Ticked |

## 7. Verify

Run [TESTING_STRATEGY.md](TESTING_STRATEGY.md) §7, at least E2E-1, E2E-2, E2E-5 and E2E-7. Logs are in CloudWatch → *Log groups* → `/aws/lambda/mergeguard-ai-pr-reviewer`, or:

```bash
aws logs tail /aws/lambda/mergeguard-ai-pr-reviewer --follow --region us-east-1
```

`aws logs tail` requires AWS CLI v2. API Gateway access logs (request metadata only) are in `/aws/apigateway/mergeguard-webhook-gateway`.

## 8. CI/CD and remote state (Phase 2, Proposed)

| Item | Design |
| --- | --- |
| `ci.yml` | Added in M1 (T-104); runs on every PR and push to `main`: `ruff`, `pytest` with coverage gate (Python 3.12), Terraform `fmt`/`validate`, `checkov` |
| `deploy.yml` | On merge to `main` or manual dispatch; GitHub environment `production` with required approval; `terraform plan`, then `apply` of that plan |
| AWS credentials in CI | GitHub OIDC → dedicated IAM deploy role restricted to this repository and branch. No long-lived access keys in GitHub secrets |
| Actions supply chain | Third-party actions pinned to full commit SHAs |
| Terraform state | S3 bucket with versioning, default encryption, and public access blocked. Locking via the S3 backend's native lock file (`use_lockfile`, recent Terraform versions; confirm against the pinned version) or a DynamoDB table on older versions |

## 9. Rollback

- **Phase 1:** check out the last known-good commit or tag and run §5 again. The stack is stateless, so rolling back code has no data migration.
- **Phase 2 (Proposed):** publish Lambda versions behind a `live` alias, so a rollback is an alias update.
- **Emergency stop:** deactivate the webhook in GitHub (*Active* unticked), or set `dry_run = true` and apply.

## 10. Operations runbook

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Every delivery returns `401` | Webhook secret differs between GitHub and SSM (EC-12) | Set both to the same value; redeliver failed deliveries |
| Every delivery returns `500` immediately after deploy | SSM parameters still hold the Terraform placeholder (`config.failed`), an import-time code error (compare RB-01), or a missing parameter | Set the real values (§3.2); otherwise check the log group for `error.unhandled` or init errors, fix and redeploy |
| GitHub shows "timed out", but the review appears | Synchronous AI call exceeded GitHub's 10 s timeout (R-01) | Expected in Phase 1; fixed by FR-016 |
| No review; logs show `github.auth_failed` | Token expired or revoked | Rotate the token ([SECURITY.md](SECURITY.md) §6) |
| No review; logs show `github.forbidden` | Repository not selected in the token, or permission missing | Edit the token's repository access and permissions |
| Review posted as comment with "changes are requested" note | PR author owns the token (EC-08) | Expected; use a dedicated bot account or the Phase 3 GitHub App |
| Reviews say "AI review unavailable" | Gemini quota, invalid model ID, or key problem | Check `ai.failed` `reason` in logs: `not_configured` means the Gemini key is missing in SSM; `bad_request` usually means an invalid model ID; `auth` means the key was rejected; `rate_limited` means quota |
| Duplicate reviews | Marker or `commit_id` mismatch (FR-014 bug) | Capture delivery IDs; add a regression test |
| Webhook not delivering at all | Webhook inactive, wrong events, or wrong URL | Check *Recent Deliveries*; redeliver `ping` |

**Redelivery:** go to *Repository → Settings → Webhooks → (webhook) → Recent Deliveries → (delivery) → Redeliver*. GitHub does not retry failed deliveries automatically.

## 11. Secret rotation

Follow [SECURITY.md](SECURITY.md) §6. Because of the cache TTL (FR-011), a rotated value takes effect within the TTL. To force it immediately, redeploy, or change any function configuration value, which recycles the execution environments.

## 12. Teardown

> **Destructive.** Removes the endpoint and deletes the secrets. Run only when you intend to decommission.

```bash
terraform -chdir=terraform destroy
```

`destroy` also deletes the three SSM parameters, including the real values you stored.

Then delete the webhook in each repository, revoke the GitHub token, and delete the Gemini API key.
