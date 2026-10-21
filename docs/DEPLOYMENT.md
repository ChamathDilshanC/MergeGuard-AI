# MergeGuard AI — Deployment and Operations

| Field | Value |
| --- | --- |
| Status | Draft v0.1 — **procedure not yet executed**. The code (M2–M3, M5, M6), the Terraform stack (T-401, T-501, T-506, M6 additions), the bootstrap stack and the deploy workflow (T-505) exist and are tested without AWS or GitHub; the first real deploy is T-402 |
| Last updated | 2026-10-21 |
| Related | [Architecture](ARCHITECTURE.md) §14 · [Security](SECURITY.md) §6 · [Testing](TESTING_STRATEGY.md) §7 |

This guide turns the brief's four deployment steps into a repeatable procedure with safer defaults. Commands are written for a POSIX shell; PowerShell equivalents are given where they differ.

## 1. Environments

| Environment | Purpose | Phase |
| --- | --- | --- |
| `sandbox` | One AWS account and region (default `us-east-1`), webhook on a sandbox repository | 1 |
| `production` | Webhook on real repositories; deployed from CI with approval | 2 (workflow written in M5, never run; §8) |

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
   - **Permissions:** *Pull requests: Read and write*, *Contents: Read-only*, *Commit statuses: Read and write*. *Metadata: Read* is added automatically. The statuses permission is needed while `commit_status_enabled` is true, which is the Terraform default; without it every status call is refused and logged (`status.failed`), and the reviews still work.
   - To let MergeGuard dismiss its old blocking reviews on a **protected branch**, the token owner must be allowed to dismiss reviews in the branch rule. Otherwise GitHub refuses and MergeGuard logs `review.dismissal_failed`.
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

### 3.4 Optional: authenticate as a GitHub App (FR-020)

Written in M6, **never run**. An App gives reviews a proper bot identity (`<app-slug>[bot]`), removes the own-PR limitation (FR-010), and replaces the personal access token with short-lived tokens scoped to one repository ([ADR-0010](ADR/0010-github-app-jwt-signing-without-dependencies.md)). Skip this section to keep using the token of §3.1.

1. **Create the App** (*Settings → Developer settings → GitHub Apps → New GitHub App*):
   - **Webhook:** active; URL is the `github_webhook_url` output (after the first apply); secret is the same webhook secret as in §3.
   - **Repository permissions:** *Pull requests: Read and write*, *Contents: Read-only*, *Commit statuses: Read and write*, *Metadata: Read-only*. Ask only for what you use: request *Contents* only with `repo_config_enabled`, and *Commit statuses* only with `commit_status_enabled`; GitHub refuses a token for a permission the App lacks.
   - **Subscribe to events:** *Pull request*.
   - **Where can it be installed:** only on your account, unless you intend to share it.
2. **Generate a private key** (a `.pem` download). Store it in SSM, then **delete the file**. Never commit it or paste it anywhere:

   ```bash
   aws ssm put-parameter --name "/mergeguard/github_app_private_key" --type SecureString --overwrite --value file://app-key.pem --region us-east-1
   shred -u app-key.pem   # or delete it securely; PowerShell: Remove-Item app-key.pem
   ```

   The key must be an unencrypted RSA key of 2048 to 8192 bits, which is what GitHub issues.
3. **Set the App ID** (shown on the App's page; not a secret) in `terraform.tfvars`, for example `github_app_id = "123456"`, and apply. The worker now reads the App key and never the personal access token, which may stay a placeholder.
4. **Install the App** on each repository (*App settings → Install App*). Webhooks now come through the App. **Remove any repository webhook** that points at the same URL, or every pull request is reviewed twice.
5. **Check** that a review shows `<app-slug>[bot]` as its author and that two repositories work.

If a review shows `github.forbidden` or `github.invalid_request` right after a token request, see the runbook (§10).

## 4. Terraform configuration

These variables live in `terraform/variables.tf` and are used by `terraform/main.tf` and `terraform/alerts.tf`, with validation for the limits stated below. Defaults reproduce the brief where the brief had a value.

| Variable | Default | Purpose |
| --- | --- | --- |
| `aws_region` | `us-east-1` | Deployment region |
| `function_name` | `mergeguard-ai-pr-reviewer` | The **receiver** Lambda (API Gateway target); also sets its log group name |
| `lambda_timeout_seconds` | `30` | Receiver timeout; must stay ≤ 30 (API Gateway HTTP API limit). The receiver only verifies and queues, so a lower value is safe |
| `lambda_memory_mb` | `256` | Receiver memory; from the brief |
| `worker_function_name` | `mergeguard-ai-pr-worker` | The SQS worker Lambda; also sets its log group name |
| `worker_timeout_seconds` | `60` | Worker timeout, 10 to 900. The queue's visibility timeout is six times this |
| `worker_memory_mb` | `256` | Worker memory |
| `worker_max_concurrency` | `2` | Most reviews that run at once (2 to 1000). Caps Gemini and GitHub traffic without needing reserved concurrency |
| `queue_max_receive_count` | `3` | Receives before a request moves to the dead-letter queue (1 to 10) |
| `commit_status_enabled` | `true` | Sets the `mergeguard/review` status → `MERGEGUARD_COMMIT_STATUS`. Needs the token's *Commit statuses* permission (§3.1) |
| `scan_allow_paths` | `[]` | Path globs whose findings are suppressed and listed in the review, for example `["tests/fixtures/**"]` → `MERGEGUARD_SCAN_ALLOW_PATHS`. Operator-only; a bare wildcard is rejected |
| `alert_email` | unset | Address for alarm and budget notifications. AWS sends a confirmation email that must be accepted |
| `monthly_budget_usd` | unset | Monthly cost budget in USD. **Unset creates no budget**: the amount is the owner's decision ([OQ-05](PRD.md#17-open-questions)). Needs `alert_email` |
| `github_app_id` | unset | GitHub App ID or client ID → `MERGEGUARD_GITHUB_APP_ID`. Unset keeps the personal access token. The private key is the `github_app_private_key` SSM parameter (§3.4) |
| `inline_comments` | `secrets` | `off`, `secrets` or `all` → `MERGEGUARD_INLINE_COMMENTS`. `secrets` posts inline comments for secret findings only. `all` also asks the model for per-line findings with the longer `review-v2` prompt, which has **not been evaluated** (T-304, [OQ-05](PRD.md#17-open-questions)); try it on a sandbox repository first |
| `repo_config_enabled` | `true` | Read `.github/mergeguard.json` from the pull request's base commit → `MERGEGUARD_REPO_CONFIG`. Needs *Contents: Read* (§4.1) |
| `repo_config_block_levels` | `["MEDIUM", "HIGH"]` | The levels a repository file may choose for `block_on` → `MERGEGUARD_REPO_CONFIG_BLOCK_LEVELS`. `["MEDIUM"]` forbids relaxing the default |
| `log_retention_days` | `14` | CloudWatch retention |
| `param_prefix` | `/mergeguard` | SSM prefix → `MERGEGUARD_PARAM_PREFIX` |
| `gemini_model` | `gemini-2.5-flash` | → `MERGEGUARD_GEMINI_MODEL` |
| `max_diff_chars` | `12000` | → `MERGEGUARD_MAX_DIFF_CHARS` |
| `max_files` | `300` | → `MERGEGUARD_MAX_FILES` |
| `log_level` | `INFO` | → `MERGEGUARD_LOG_LEVEL` |
| `dry_run` | `false` | → `MERGEGUARD_DRY_RUN` |
| `throttling_rate_limit` / `throttling_burst_limit` | `10` / `20` | API Gateway stage throttling |
| `reserved_concurrency` | unset (`-1`, unreserved) | Optional cap on the receiver. Set it once the account's concurrency quota allows ([SECURITY.md](SECURITY.md) §5.9) |

Put local overrides in `terraform/terraform.tfvars`, which must be git-ignored. It must never contain secrets.

### 4.1 Repository configuration file

Optional, JSON, at `.github/mergeguard.json` in the **base branch** of the repository. A change takes effect after it is merged, not on the pull request that proposes it ([ADR-0011](ADR/0011-repository-config-from-the-base-commit.md)).

```json
{
  "version": 1,
  "block_on": "HIGH",
  "ai_review": true,
  "ignore_paths": ["docs/**", "**/*.lock"],
  "inline_comments": "secrets",
  "language": "en"
}
```

| Key | Meaning | Bound |
| --- | --- | --- |
| `version` | Must be `1` if present | — |
| `block_on` | Lowest AI risk level that requests changes. AI bugs and secret findings always block | One of the operator's `repo_config_block_levels` |
| `ai_review` | `false` keeps this repository's diffs from the AI (nothing is posted when the scan is clean) | — |
| `ignore_paths` | Globs kept from the AI. They are still scanned for secrets | At most 50, relative, not only wildcards |
| `inline_comments` | `off`, `secrets` or `all` | Never above the operator's mode |
| `language` | `en` | Only `en` until [OQ-03](PRD.md#17-open-questions) is decided |

It cannot disable the secret scan or add suppressions. Any invalid key or value makes the **whole file** ignored: the defaults apply and the review says so, with a reason (`invalid_json`, `too_large`, `unknown_key`, `unsupported_version`, `invalid_field` or `out_of_bounds`). Keep it under 16 KB.

## 5. Deploy

```bash
terraform -chdir=terraform init
terraform -chdir=terraform test          # no credentials needed; mocked AWS provider
terraform -chdir=terraform plan -out=tfplan
# Review the plan: 27 resources to add with the defaults (table below)
terraform -chdir=terraform apply tfplan
terraform -chdir=terraform output -raw github_webhook_url
```

The brief used `terraform apply -auto-approve`. Reviewing a saved plan is the default here, because the plan shows exactly which IAM permissions are about to be granted.

**What the plan creates** (verified without credentials by `terraform test`; 27 resources with `alert_email` and `monthly_budget_usd` unset):

| Resource | Purpose |
| --- | --- |
| `aws_ssm_parameter.secret` (4) | `github_token`, `webhook_secret`, `gemini_api_key` and (M6) `github_app_private_key` as `SecureString` placeholders (§3) |
| `aws_iam_role.lambda`, `aws_iam_role_policy.lambda` | Receiver role: its own logs, the webhook secret, `sqs:SendMessage` on the review queue ([SECURITY.md](SECURITY.md) §5.3) |
| `aws_iam_role.worker`, `aws_iam_role_policy.worker` | Worker role: its own logs, the GitHub token and the Gemini key, and the review queue |
| `aws_sqs_queue.review_requests`, `.dlq`, `aws_sqs_queue_redrive_allow_policy.dlq` | The review queue (1 day retention, SSE-SQS) and its dead-letter queue (14 days); only the review queue may redrive into it |
| `aws_cloudwatch_log_group.lambda`, `.worker`, `.api_access` | Receiver, worker and API Gateway access logs, 14-day retention |
| `aws_lambda_function.reviewer`, `.worker` | The receiver (`lambda_function.lambda_handler`) and the worker (`worker_function.lambda_handler`), Python 3.12, one shared zip built from `src/` |
| `aws_lambda_event_source_mapping.worker` | SQS to worker, one message per invocation, item failures reported, concurrency capped |
| `aws_apigatewayv2_api.webhook`, `_stage.default`, `_integration.lambda`, `_route.webhook` | HTTP API, `$default` stage with throttling and access logs, `POST /webhook` only |
| `aws_lambda_permission.api` | Lets only `POST /webhook` of this API invoke the receiver |
| `aws_sns_topic.alerts` | Where the alarms publish |
| `aws_cloudwatch_metric_alarm` (4) | Dead-letter queue not empty; webhook `5xx`; receiver and worker Lambda errors |
| `aws_sns_topic_subscription.email`, `aws_budgets_budget.monthly` | Only when `alert_email` and `monthly_budget_usd` are set |

**Test the alarm path** (needs `alert_email`): `aws cloudwatch set-alarm-state --alarm-name mergeguard-review-dlq-not-empty --state-value ALARM --state-reason test` should send an email within a minute. Repeat for the other alarms ([TESTING_STRATEGY.md](TESTING_STRATEGY.md) E2E-13).

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

Run [TESTING_STRATEGY.md](TESTING_STRATEGY.md) §7, at least E2E-1, E2E-2, E2E-5, E2E-7 and E2E-10. The receiver and the worker log separately: the receiver shows `webhook.received` and `queue.enqueued`, the worker shows `worker.received`, the review events and `worker.completed`. Both carry the same `delivery_id`.

```bash
aws logs tail /aws/lambda/mergeguard-ai-pr-reviewer --follow --region us-east-1   # receiver
aws logs tail /aws/lambda/mergeguard-ai-pr-worker --follow --region us-east-1     # worker
```

`aws logs tail` requires AWS CLI v2. API Gateway access logs (request metadata only) are in `/aws/apigateway/mergeguard-webhook-gateway`. *Recent Deliveries* should now show `202` for every reviewable pull request event.

## 8. CI/CD and remote state (Phase 2; written in M5, never run)

| Item | Design |
| --- | --- |
| `ci.yml` | Added in M1 (T-104), extended in M5. Runs on every PR and push to `main`: `ruff`, `pytest` with the coverage gate (Python 3.12), Terraform `fmt`, `validate` and plan tests for the stack and for `terraform/bootstrap`, `checkov` |
| `deploy.yml` | **Manual** (`workflow_dispatch`), on `main` only. A `plan` job in the `production-plan` environment saves a plan and prints it to the job summary. An `apply` job in the `production` environment, which needs required reviewers, applies exactly that plan |
| AWS credentials in CI | GitHub OIDC to the role `mergeguard-github-deploy`, which only this repository's two environments can assume. No long-lived keys in GitHub; the workflow holds only non-secret identifiers as repository variables |
| Actions supply chain | Third-party actions pinned to full commit SHAs, checked by a test |
| Terraform state | S3 bucket with versioning, default encryption, public access blocked and TLS-only access. Locking uses the S3 backend's native lock file (`use_lockfile`, Terraform 1.11 or later). **Opt-in:** local state stays the default, and CI selects the S3 backend at run time ([ADR-0009](ADR/0009-ci-deploy-with-oidc-and-opt-in-remote-state.md)). [OQ-08](PRD.md#17-open-questions) stays open |

The residual risks (the plan job can also apply, the role can create `mergeguard_*` IAM roles, the provider reads the real SSM values while it refreshes) are in [SECURITY.md](SECURITY.md) §5.10.

### 8.1 One-time setup (maintainer)

None of this has been run. Review each step before running it.

1. **Create the state bucket and the deploy role** with the bootstrap stack. It uses local state, which you keep, and CI never applies it.

   ```bash
   terraform -chdir=terraform/bootstrap init
   terraform -chdir=terraform/bootstrap plan -var 'state_bucket_name=<globally-unique-name>' -out=tfplan
   # Review the plan, then:
   terraform -chdir=terraform/bootstrap apply tfplan
   terraform -chdir=terraform/bootstrap output
   ```

   If the account already has a GitHub OIDC provider (an account can hold one), add `-var create_oidc_provider=false`. If the main stack does not use the defaults, pass the same `aws_region` and `param_prefix`, because the deploy role is scoped to that region and SSM prefix, and a mismatch shows up as access denied during the first deploy. Never commit the bootstrap state file or `tfplan`.

2. **Set three repository variables** (*Settings → Secrets and variables → Actions → Variables*; none of them is a secret): `AWS_DEPLOY_ROLE_ARN` (output `deploy_role_arn`), `AWS_REGION`, and `TF_STATE_BUCKET` (output `state_bucket_name`).
3. **Create two environments** (*Settings → Environments*): `production-plan` with no reviewers, and `production` with **required reviewers**. Limit both to the `main` branch (*Deployment branches*). The names must match the bootstrap variables `plan_environment` and `apply_environment`.
4. **Protect `main`**: require pull requests and the CI checks. This closes most of TM-16.
5. **If the stack already exists in local state, migrate it first**, or the first CI deploy will plan to create everything again:

   ```bash
   cat > terraform/backend_override.tf <<'EOF'
   terraform {
     backend "s3" {}
   }
   EOF
   terraform -chdir=terraform init -migrate-state \
     -backend-config="bucket=<state-bucket>" -backend-config="key=mergeguard/terraform.tfstate" \
     -backend-config="region=us-east-1" -backend-config="encrypt=true" -backend-config="use_lockfile=true"
   rm terraform/backend_override.tf
   ```

   `backend_override.tf` is git-ignored. Keep exactly one place that applies the stack, either your machine or CI, to avoid state conflicts.
6. **Run the workflow**: *Actions → Deploy → Run workflow* on `main`. Read the plan in the job summary, then approve the `production` environment.

To deploy automatically after a merge, add `push: branches: [main]` to `deploy.yml` once the variables exist and a manual run has succeeded. It is deliberately not enabled now: nothing is deployed, and every push would fail until the variables exist.

## 9. Rollback

- **Phase 1:** check out the last known-good commit or tag and run §5 again. The stack is stateless, so rolling back code has no data migration.
- **Phase 2 (Proposed):** publish Lambda versions behind a `live` alias, so a rollback is an alias update.
- **Emergency stop:** deactivate the webhook in GitHub (*Active* unticked), or set `dry_run = true` and apply. A dry run still runs the pipeline but writes nothing to GitHub: no review, no commit status, no dismissal.
- **Pause the worker only:** requests wait in the queue for up to one day (`aws lambda update-event-source-mapping --uuid <id> --no-enabled`, then enable it again). Older requests expire.
- **Back to the synchronous Phase 1 design:** not supported by this stack (the receiver's role no longer holds the token). Check out a commit from before M5 and redeploy.

## 10. Operations runbook

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Every delivery returns `401` | Webhook secret differs between GitHub and SSM (EC-12) | Set both to the same value; redeliver failed deliveries |
| Every delivery returns `500` immediately after deploy | SSM parameters still hold the Terraform placeholder (`config.failed`), an import-time code error (compare RB-01), or a missing parameter | Set the real values (§3.2); otherwise check the log group for `error.unhandled` or init errors, fix and redeploy |
| GitHub shows "timed out", but the review appears | Synchronous AI call exceeded GitHub's 10 s timeout (R-01) | Phase 1 only. With the queue, *Recent Deliveries* shows `202`; if timeouts still appear, check the receiver's log for `queue.failed` or slow SSM calls |
| Alarm `mergeguard-review-dlq-not-empty`, or messages in the dead-letter queue | A review request failed three times (worker log: `github.*`, `config.failed`, `error.unhandled`, `worker.rejected`) | Find the cause with the `delivery_id` in the worker log group and fix it (expired token, missing permission, bad setting). Then redrive: *SQS → the DLQ → Start DLQ redrive*, or `aws sqs start-message-move-task --source-arn <dlq-arn>`. Messages are metadata only and expire after 14 days |
| Alarm `mergeguard-webhook-5xx` | The receiver returned `500`: the webhook secret is missing or a placeholder (`config.failed`), SQS refused the send (`queue.failed`), or an unexpected error | Fix the cause, then redeliver the failed deliveries from GitHub (it does not retry) |
| `github.auth_failed`, `github.forbidden` or `github.invalid_request` on the first call of a review in App mode | The token request failed: `401` the key or App ID is wrong; `404` the App is not installed on that repository; `422` a permission the code requests was not granted to the App (*Contents* with `repo_config_enabled`, *Commit statuses* with `commit_status_enabled`) | Grant the permission in the App's settings and accept it on the installation, or switch the matching setting off |
| `config.failed` in App mode | The App private key is a placeholder, unreadable, below 2048 bits, encrypted or not RSA, or the webhook did not come through an App installation | Store a valid key (§3.4); make sure the webhook is the App's, not a repository webhook |
| Pull requests are reviewed twice | Both a repository webhook and the App's webhook point at the URL | Remove the repository webhook (§3.4 step 4) |
| A review says the repository configuration was ignored | The file in the base branch is invalid or outside the bounds; the reason is in the notice and in `repo_config.invalid` | Fix the file on the base branch (§4.1) |
| No inline comments appear | `inline_comments` is `off`, the repository lowered it, or GitHub refused the comments (`review.inline_dropped`; the review itself is posted) | Check the mode and the log; the summary review lists every finding |
| Alarm `mergeguard-*-lambda-errors` | A timeout, an out-of-memory kill or an import-time crash that the handler could not catch | Check the log group for `Task timed out` or a traceback; raise `worker_timeout_seconds` or `worker_memory_mb`, or fix the code |
| Check `mergeguard/review` stays `pending` | The worker is down or retrying, or the final status call failed | Look in the dead-letter queue and the worker log. A retry sets the final status without a new review. As a last resort, redeliver the webhook |
| No `mergeguard/review` status at all | `commit_status_enabled` is false, the token lacks *Commit statuses: Read and write* (log `status.failed`, `forbidden`), or the PR has no reviewable patch | Enable the setting or add the permission |
| An old `REQUEST_CHANGES` review still blocks the merge | The dismissal was refused (log `review.dismissal_failed`): the token owner may not dismiss reviews on that protected branch, or the review is not MergeGuard's | Allow the token owner in the branch rule, or dismiss it by hand; the next clean push retries |
| A review lists suppressed findings | An inline `mergeguard:ignore-secret` marker or `scan_allow_paths` matched | Expected. Check that each listed line really is a fake or an example |
| No review; logs show `github.auth_failed` | Token expired or revoked | Rotate the token ([SECURITY.md](SECURITY.md) §6) |
| No review; logs show `github.forbidden` | Repository not selected in the token, or permission missing | Edit the token's repository access and permissions |
| Review posted as comment with "changes are requested" note | PR author owns the token (EC-08) | Expected; use a dedicated bot account or the Phase 3 GitHub App |
| Reviews say "AI review unavailable" | Gemini quota, invalid model ID, or key problem | Check `ai.failed` `reason` in logs: `not_configured` means the Gemini key is missing in SSM; `bad_request` usually means an invalid model ID; `auth` means the key was rejected; `rate_limited` means quota |
| Duplicate reviews | Marker or `commit_id` mismatch (FR-014 bug) | Capture delivery IDs; add a regression test |
| Webhook not delivering at all | Webhook inactive, wrong events, or wrong URL | Check *Recent Deliveries*; redeliver `ping` |

**Redelivery:** go to *Repository → Settings → Webhooks → (webhook) → Recent Deliveries → (delivery) → Redeliver*. GitHub does not retry failed deliveries automatically.

## 11. Secret rotation

Follow [SECURITY.md](SECURITY.md) §6. Because of the cache TTL (FR-011), a rotated value takes effect within the TTL in each function. To force it immediately, redeploy, or change a configuration value of both functions, which recycles their execution environments.

## 12. Teardown

> **Destructive.** Removes the endpoint and deletes the secrets. Run only when you intend to decommission.

```bash
terraform -chdir=terraform destroy
```

`destroy` also deletes the three SSM parameters, including the real values you stored, and both queues with any request still waiting in them. It does not touch the state bucket or the deploy role: they belong to the bootstrap stack, whose bucket has `prevent_destroy` and must be emptied and unprotected by hand before it can be removed.

Then delete the webhook in each repository, revoke the GitHub token, and delete the Gemini API key.
