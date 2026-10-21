# MergeGuard AI — Security

| Field | Value |
| --- | --- |
| Status | Draft v0.1 — webhook verification, secret loading, secret scanning, review sanitisation, log redaction and duplicate handling implemented in M2; AI data handling and prompt-injection defences in M3 (mocked Gemini only); IAM, throttling and logging written in Terraform (M4); the receiver/worker split with per-function roles, scanner suppressions, alarms and budget, and the OIDC deploy pipeline in M5; GitHub App authentication with a hand-written RS256 signer, inline comments and base-commit repository configuration in M6. **Nothing is deployed and nothing has been applied** |
| Last updated | 2026-10-21 |
| Owner | [@ChamathDilshanC](https://github.com/ChamathDilshanC) |
| Related | [Architecture](ARCHITECTURE.md) · [AI system](AI_SYSTEM.md) · [Deployment](DEPLOYMENT.md) |

MergeGuard is a security tool. It handles three high-value secrets and other people's source code, and it exposes a public endpoint. This document defines what must be protected, the threats, and the controls each component must implement.

## 1. Security principles

1. **Verify before trusting.** No webhook content is parsed before the signature is verified.
2. **Fail closed on security checks, fail open on advice.** Missing secrets or a bad signature stop processing. An AI outage only produces an advisory comment.
3. **Least privilege everywhere.** IAM, GitHub token scope, and data sent to third parties are each the minimum that works.
4. **Secrets are never displayed.** Secret values never appear in code, Terraform state, logs, review comments, or test fixtures.
5. **Untrusted input stays data.** Diff content, file names, and PR metadata are attacker-controllable; the model's output is too.
6. **Humans decide merges.** MergeGuard never approves or merges.

## 2. Assets and data classification

| Asset | Classification | Where it lives | Notes |
| --- | --- | --- | --- |
| GitHub token (`/mergeguard/github_token`) | Restricted | SSM `SecureString`, Lambda memory | Write access to PRs of covered repositories |
| Webhook secret (`/mergeguard/webhook_secret`) | Restricted | SSM, GitHub webhook settings, Lambda memory | Lets anyone holding it forge deliveries |
| Gemini API key (`/mergeguard/gemini_api_key`) | Restricted | SSM, Lambda memory | Quota and billing exposure |
| PR diffs and file names | Confidential (repository-dependent) | GitHub; transient in Lambda memory; sent to Gemini only when the scan is clean | Not persisted by MergeGuard |
| Secret findings | Confidential | Review comments (rule, file, line only) | Values never rendered |
| Logs | Internal | CloudWatch Logs | Metadata only (FR-013) |
| Review requests | Internal | SQS queue and dead-letter queue (SSE-SQS), kept up to 1 and 14 days | Metadata only: delivery ID, repository, PR number, head SHA, action (API_SPEC.md §6). Never code |
| Terraform state | Internal | Local file by default, or the encrypted S3 bucket when CI deploys (ADR-0009) | Contains no secret values by design |
| Deploy role (`mergeguard-github-deploy`) | Restricted | AWS IAM, assumed through GitHub OIDC | Can create and change the stack's resources, including its IAM roles (§5.10) |

## 3. Trust boundaries

```mermaid
flowchart LR
    subgraph internet["Internet - untrusted"]
        att["Anyone"]
        gh["GitHub"]
    end
    subgraph aws["AWS account - trusted"]
        api["API Gateway"]
        rcv["Receiver Lambda"]
        q["SQS queue + DLQ"]
        wrk["Worker Lambda"]
        ssm["SSM Parameter Store"]
        cw["CloudWatch Logs"]
    end
    subgraph google["Google - third party"]
        gem["Gemini API"]
    end
    att -->|"unsigned or forged requests"| api
    gh -->|"signed webhook"| api
    api --> rcv
    rcv -->|"webhook secret only"| ssm
    rcv -->|"metadata-only message"| q
    q --> wrk
    wrk -->|"token and Gemini key only"| ssm
    rcv --> cw
    wrk --> cw
    wrk -->|"diff, only after clean scan"| gem
    wrk -->|"token-authenticated API calls"| gh
```

- **B1, Internet → API Gateway:** anyone can reach the endpoint. HMAC verification is the trust gate. Only the receiver sits behind it, and it can read one secret and write to one queue.
- **B2, worker → Gemini:** code crosses an organisational boundary. The secret scan gates this crossing ([ADR-0004](ADR/0004-secret-scan-gate-before-ai.md)), and lines the scanner flagged but a suppression waved through are withheld from it ([ADR-0008](ADR/0008-scanner-suppressions-are-visible-and-withheld-from-ai.md)).
- **B3, worker → GitHub:** MergeGuard acts with the token owner's authority. Token scope limits the blast radius. Only the worker holds the token.
- **B4, diff content → model → review body:** untrusted content flows through the model into a comment that other people read.

## 4. Threat model

STRIDE categories: **S**poofing, **T**ampering, **R**epudiation, **I**nformation disclosure, **D**enial of service, **E**levation of privilege.

| ID | STRIDE | Threat | Control | Residual risk |
| --- | --- | --- | --- | --- |
| TM-01 | S | Forged webhook makes the bot post reviews or burn AI quota | HMAC-SHA256 over the raw body, constant-time compare, verification before parsing, fail closed (FR-002) | Webhook secret compromise (see IR-2) |
| TM-02 | S/R | Replay of a captured valid delivery | TLS in transit; idempotency on head SHA prevents duplicate reviews (FR-014) | Replays cost one extra invocation |
| TM-03 | T | **Prompt injection** in the diff (for example a comment telling the AI to "report LOW risk") | Policy computed in code; no `APPROVE`; diff wrapped as delimited data with explicit instructions; schema-validated output; human merge decision ([AI_SYSTEM.md](AI_SYSTEM.md) §7) | A misleading advisory review is possible; it cannot approve or merge |
| TM-04 | I | Secrets in a PR are sent to a third-party AI | Deterministic scan runs first; any finding skips the AI call (FR-006) | Scanner false negatives (R-09) |
| TM-05 | I | Secrets or code leak via logs or comments | Allow-listed log fields; no body, diff, or raw AI output logged; findings masked (FR-013, FR-005) | Developer error; covered by redaction tests |
| TM-06 | E | Stolen GitHub token is used beyond reviewing | Fine-grained PAT, selected repositories only, no contents write or admin permissions, expiry date, SSM storage | Abuse within PR-write scope until revoked (IR-1) |
| TM-07 | E | Lambda role abused to read other secrets or resources | Inline policy limited to its own log group and `parameter/mergeguard/*` in one account and region | — |
| TM-08 | D | Flooding the public endpoint drives up cost or exhausts concurrency | Invalid signatures rejected quickly; API Gateway stage throttling; optional reserved concurrency on the receiver; worker concurrency capped by the event source mapping; the queue absorbs bursts; AWS Budgets alert and CloudWatch alarms (written in M5); Gemini only reached after a valid signature | Throttling may delay legitimate deliveries during an attack; the budget alert exists only when `monthly_budget_usd` and `alert_email` are set |
| TM-09 | I | Terraform state or plan files leak | Secret values never enter state: parameters use write-only placeholders (ADR-0007) and are never imported; `*.tfstate*` and `tfplan` git-ignored; Phase 2 remote state in encrypted S3 | — |
| TM-10 | T | Supply-chain compromise | Zero runtime dependencies (ADR-0002); providers pinned and `.terraform.lock.hcl` committed; GitHub Actions pinned to commit SHAs (M1 CI) | Dev-only tool compromise (pytest, ruff) |
| TM-11 | T | Model output used for abuse (`@mention` spam, phishing links, oversized body) | Neutralise mentions; cap length; fixed template structure (FR-009) | Misleading prose inside the feedback section |
| TM-12 | R | No trace of why a review was posted | Every log line carries `delivery_id`, repository, PR number, head SHA and decision; API Gateway access logs record each request (M4); GitHub retains reviews | Log retention window (14 days) |
| TM-13 | D | External contributors open many PRs on a public repository and exhaust AI quota | Throttling; AI failures fail open; Phase 3 per-repository config can disable AI for forks | Quota exhaustion degrades reviews to scan-only |
| TM-14 | S/T | A PR author posts their own review containing a copy of MergeGuard's hidden marker for the head SHA, so the duplicate check skips the real security review | A review counts as a duplicate only if its author is the token owner (`GET /user`), its `commit_id` matches, and it carries the marker (FR-014). The same author and marker checks decide which reviews MergeGuard dismisses (FR-018) | Phase 3 GitHub App tokens cannot call `GET /user`; the identity check must be redone then (ADR-0006) |
| TM-15 | T/E | A PR author puts `mergeguard:ignore-secret` next to a real credential to get past the gate | The marker only affects its own line; every suppression is listed in the review with rule, file, line and method; suppressed lines are withheld from the AI; the path allow-list is operator-only and fails closed ([ADR-0008](ADR/0008-scanner-suppressions-are-visible-and-withheld-from-ai.md)) | The credential is **not blocked** and reaches the branch history; a human has to read the notice. GitHub push protection is the backstop |
| TM-16 | E/T | The CI deploy pipeline is abused: a changed workflow applies without approval, or the deploy role is misused | OIDC trust limited to this repository's two environments; no AWS keys in GitHub; the apply job runs in an environment with required reviewers and applies only the saved plan; actions pinned to commit SHAs; deploys run only on `main`; the role is scoped to `mergeguard*` in one account and region (§5.10, [ADR-0009](ADR/0009-ci-deploy-with-oidc-and-opt-in-remote-state.md)) | The plan job holds the same role, so anyone who can change the workflow on `main` can skip the approval. Needs branch protection on `main` and main-only environments. The role can create IAM roles under `mergeguard_*` |
| TM-17 | T | A forged or malformed message in the queue triggers unwanted work | Only the receiver's role may send; the worker validates every field like a webhook payload and re-fetches everything from GitHub; messages hold metadata only; a bad message ends in the dead-letter queue | Someone holding `sqs:SendMessage` could trigger a review of a pull request the token already covers |
| TM-18 | S/E | The GitHub App's private key is stolen, so an attacker mints installation tokens for every installation | Stored as an SSM SecureString read only by the worker role; the signer never logs it; tokens are scoped to one repository, carry only `pull_requests`, `contents` (read) and `statuses`, and last at most an hour; a rotated key is picked up without a restart (§5.11, IR-5) | The key sits in worker memory; the signer is hand-written and not independently audited (ADR-0010). Until it is revoked, a thief can act as the App |
| TM-19 | T/E | A pull request edits `.github/mergeguard.json` to relax or switch off its own review | The file is read from the PR's base commit, never the head; the operator sets the bounds; the file cannot touch the secret scan or add suppressions; an unknown key or out-of-bounds value discards the whole file and says so in the review ([ADR-0011](ADR/0011-repository-config-from-the-base-commit.md)) | Someone with write access to the base branch can relax the repository's own thresholds or exclude paths from the AI; the review lists what was applied |
| TM-20 | T/I | AI inline comments spam, mislead or point at lines the author never wrote, or leak a flagged value | Anchored only to diff lines that were sent to the model; withheld lines are never anchored; text sanitised like all model output; at most 10 AI and 25 secret comments; secret comments name the rule, never the value; AI findings are opt-in (`all`) | An anchored but wrong comment is possible; per-line accuracy is unmeasured until T-304 |

## 5. Controls by component

### 5.1 Webhook verification (FR-002)

- Read the secret from SSM. If it is missing or empty, fail closed.
- Decode the body exactly once: base64 if `isBase64Encoded`, otherwise UTF-8 bytes of `body`. Compute `"sha256=" + HMAC_SHA256(secret, body_bytes).hexdigest()`.
- Compare with `hmac.compare_digest`. Never use `==`. Never short-circuit on header length. Compare as bytes, so a non-ASCII header is rejected instead of raising.
- Do not log the signature header or the body. Log `webhook.rejected` with `delivery_id` only.

### 5.2 GitHub token

- Phase 1: a fine-grained personal access token with the permissions in [API_SPEC.md](API_SPEC.md) §3.3, restricted to the target repositories, with an expiry date.
- Prefer a dedicated machine account over the maintainer's personal account ([OQ-06](PRD.md#17-open-questions)). This also avoids the own-PR `422` limitation.
- Phase 3 (M6): GitHub App installation tokens, minted per repository with minimal permissions and valid for at most an hour ([ADR-0006](ADR/0006-github-identity-pat-then-app.md), [ADR-0010](ADR/0010-github-app-jwt-signing-without-dependencies.md), §5.11). Set `github_app_id` to switch; the personal access token is then never read.

### 5.3 IAM (FR-012)

Two inline policies, one per function, so each can do only its own job. Both are implemented in `terraform/main.tf` and enforced by `terraform/tests/plan.tftest.hcl`, which fails if any action or resource changes. `tests/unit/test_infra_contract.py` checks that each policy names only the secrets its function reads. ARNs are built from data sources, never wildcards.

**Receiver** (`mergeguard_lambda_policy`; API Gateway → verify → queue):

| Action | Resource |
| --- | --- |
| `logs:CreateLogStream`, `logs:PutLogEvents` | `arn:aws:logs:<region>:<account>:log-group:/aws/lambda/mergeguard-ai-pr-reviewer:*` |
| `ssm:GetParameter`, `ssm:GetParameters` | `arn:aws:ssm:<region>:<account>:parameter/mergeguard/webhook_secret` |
| `sqs:SendMessage` | `arn:aws:sqs:<region>:<account>:mergeguard-review-requests` |

**Worker** (`mergeguard_worker_policy`; queue → review):

| Action | Resource |
| --- | --- |
| `logs:CreateLogStream`, `logs:PutLogEvents` | `arn:aws:logs:<region>:<account>:log-group:/aws/lambda/mergeguard-ai-pr-worker:*` |
| `ssm:GetParameter`, `ssm:GetParameters` | `…:parameter/mergeguard/github_token`, `…:parameter/mergeguard/github_app_private_key` and `…:parameter/mergeguard/gemini_api_key` |
| `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` | `arn:aws:sqs:<region>:<account>:mergeguard-review-requests` |

The receiver cannot read the GitHub token or the Gemini key, and the worker cannot read the webhook secret, so a bug in the public-facing function cannot reach the token. `kms:Decrypt` is needed only if a customer-managed key replaces `aws/ssm`; it would be scoped to that key ARN. `logs:CreateLogGroup` is not needed because Terraform creates the log groups. The Lambda permission for API Gateway uses `source_arn` scoped to `POST /webhook` of this API.

The queues use SSE-SQS. The dead-letter queue accepts redrives only from the review queue (`redrive_allow_policy`).

### 5.4 Secrets management

- Terraform creates the parameters as `SecureString` with a public placeholder, through the write-only `value_wo` argument, so no value ever enters plan or state ([ADR-0007](ADR/0007-ssm-placeholders-with-write-only-values.md)). Never `terraform import` them: an import reads the real value into state.
- Operators store the real values with the AWS CLI (`--overwrite`, [DEPLOYMENT.md](DEPLOYMENT.md) §3.2), reading them interactively so they stay out of shell history.
- The Lambda treats a placeholder value as not set. Placeholder core secrets fail closed, so the public placeholder can never act as a webhook secret. A placeholder Gemini key means no diff is sent to Gemini.
- The webhook secret is random: at least 32 bytes from a CSPRNG, for example `secrets.token_hex(32)`. Do not use memorable strings like the brief's example.
- Each Lambda caches the values it reads per execution environment with a TTL (15 minutes), so a rotation takes effect without a redeploy.

### 5.5 AI data handling

- Only the size-limited diff (file names and patch hunks) is sent. PR title, description, author, and repository metadata are **not** sent in Phase 1.
- Nothing is sent when the secret scan finds anything. Lines that the scanner flagged but a suppression waved through (FR-019) are replaced by a fixed stub before the diff is bundled, so they never leave AWS either ([ADR-0008](ADR/0008-scanner-suppressions-are-visible-and-withheld-from-ai.md)).
- The Gemini key is read from SSM only when an AI review runs. A missing key degrades to the AI-unavailable comment rather than a `500`, so the secret gate never depends on the AI provider.
- The owner must check the current Gemini API terms for the chosen tier before reviewing proprietary code. Under Google's published terms, content sent to unpaid tiers may be used to improve Google's products. Tracked as [OQ-05](PRD.md#17-open-questions); see [AI_SYSTEM.md](AI_SYSTEM.md) §3.

### 5.6 Logging and redaction (FR-013)

- Logs are emitted only through `logging_util`, which accepts the allow-listed fields in [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) §7.
- Exceptions are logged by type and a sanitised message. Never log a URL that could carry credentials, or a response body from GitHub or Gemini.

### 5.7 Abuse and cost controls

- API Gateway `$default` stage throttling (rate 10 requests/s, burst 20, tunable).
- The queue decouples acceptance from processing, and `worker_max_concurrency` (default 2) caps how many reviews run at once, so a flood cannot turn into a flood of Gemini or GitHub calls.
- API Gateway access logs (M4) record request metadata (request ID, source IP, route, status), never bodies.
- The Lambda permission allows only `POST /webhook` of this API (M4).
- Optional reserved concurrency for the receiver (`reserved_concurrency`, unset by default). New AWS accounts can have low concurrency quotas that prevent reserving capacity, so check *Service Quotas* first.
- CloudWatch alarms (written in M5, T-506): the dead-letter queue is not empty, the webhook returned `5xx`, and either Lambda reported an error. They publish to an SNS topic; `alert_email` subscribes an address.
- An AWS Budgets alert on account-wide monthly cost (80% of actual, 100% of forecast), created when `monthly_budget_usd` and `alert_email` are set. The amount is the owner's decision (OQ-05).

### 5.8 Supply chain and repository hygiene

- No runtime dependencies (ADR-0002), enforced by an import check in the test suite. Dev dependencies are pinned in `requirements-dev.txt`, CI tools are pinned in the workflow, and new versions follow a 14-day release cooldown.
- Commit `.terraform.lock.hcl`. Never commit `.terraform/`, `*.tfstate*`, `tfplan`, `terraform/build/lambda.zip`, `.env*`, or credential files.
- Test fixtures use only well-known fake values, for example AWS's documented example key ID `AKIAIOSFODNN7EXAMPLE` ([TESTING_STRATEGY.md](TESTING_STRATEGY.md) §4).

### 5.9 Accepted checkov findings

checkov runs in CI on every PR (TESTING_STRATEGY.md §5). Each accepted finding is skipped **inline on its resource**, with the reason next to it. There are no blanket skips. API Gateway access logging (CKV_AWS_76) was fixed rather than skipped.

**Confirmed by CI.** PR #4 (M4): 27 passed, 0 failed, 11 skipped, after the secrets-scan false positive (a Terraform local named like a credential) was fixed by renaming it. PR #5 (M5): 78 passed, 0 failed, 33 skipped over `terraform/` and `terraform/bootstrap/`. PR #6 (M6): 79 passed, 0 failed, 34 skipped, no parsing errors; the one extra skip is `CKV_AWS_337` on the new `github_app_private_key` parameter, for the same reason as the other three SSM parameters (ADR-0003). Checkov could not be installed locally in M5 (very slow package downloads), so CI was its first run on the M5 resources. Every skip below was triggered, so none is unused. The M4 Lambda skips now apply to both Lambda functions.

**A parsing error is a finding.** The first M5 run reported `Parsing errors: 1`: checkov's HCL parser could not read `terraform/variables.tf` (a `for` condition continued across lines), so it **skipped the file and still exited 0**. The condition was split into one-line validations, and the CI policy-scan step now fails if checkov reports a parsing error.

**Confirmed in CI (M4)**

| Check | Resource | Why it is accepted |
| --- | --- | --- |
| CKV_AWS_50 (X-Ray tracing) | Lambda | Tracing is not planned for Phase 1 (ARCHITECTURE.md §12) and would need X-Ray permissions outside §5.3 |
| CKV_AWS_115 (reserved concurrency) | Lambda | Optional (`reserved_concurrency`), because new accounts cannot reserve any; stage throttling caps load (TM-08) |
| CKV_AWS_116 (dead-letter queue) | Lambda | Invoked synchronously by API Gateway, so a DLQ never receives anything; Phase 2 adds SQS with a DLQ (ADR-0005). In M5 the worker has none either: SQS invokes it and the queue's redrive policy dead-letters failures |
| CKV_AWS_117 (VPC) | Lambda | Calls only GitHub, Gemini and SSM over HTTPS and reaches no private resources; a VPC adds NAT cost and no protection |
| CKV_AWS_173 (env-var CMK) | Lambda | Environment variables hold non-secret settings only; secrets come from SSM at runtime |
| CKV_AWS_272 (code signing) | Lambda | The zip is built by Terraform from a reviewed commit; revisit with the CI deploy pipeline (T-505) |
| CKV_AWS_337 (SSM CMK) | SSM parameters | ADR-0003 keeps the AWS-managed `aws/ssm` key |
| CKV_AWS_158 (log group CMK) | Both log groups | Metadata-only logs, already encrypted at rest by CloudWatch Logs |
| CKV_AWS_338 (1-year retention) | Both log groups | 14 days is the documented default (DEPLOYMENT.md §4); raise `log_retention_days` if an audit needs more |
| CKV_AWS_309 (route authorisation) | `POST /webhook` route | GitHub cannot send IAM or JWT credentials; the Lambda verifies the HMAC signature first (FR-002, TM-01) |
| CKV2_AWS_51 (client certificate) | `$default` stage | Client certificates apply to HTTP backends of WebSocket APIs; this HTTP API calls Lambda directly |

**Confirmed in CI (M5)**

| Check | Resource | Why it is accepted |
| --- | --- | --- |
| CKV_AWS_26 (SNS encryption) | `aws_sns_topic.alerts` | CloudWatch alarms cannot publish to a topic encrypted with the AWS-managed key, and a customer-managed key adds cost and a key policy; messages hold alarm names and values only |
| CKV_AWS_18, CKV_AWS_144, CKV_AWS_145, CKV2_AWS_62 | State bucket (`terraform/bootstrap`) | No access-log bucket, replication, customer-managed key or event notifications: state holds no secret values (ADR-0007), is versioned, and can be rebuilt |
| CKV_AWS_355, CKV_AWS_290, CKV_AWS_287, CKV_AWS_289, CKV_AWS_288 | Deploy role policy (`terraform/bootstrap`) | A few AWS actions have no resource-level permissions; every write is scoped to `mergeguard*` in one account and region; the residual risks are in §5.10 |

The SQS queues use SSE-SQS (`sqs_managed_sse_enabled`), and the queues, alarms and budget carry no skip: checkov passed them as written.

### 5.10 CI deploy pipeline (T-505, ADR-0009)

Written in M5, **never run**. It is checked with `actionlint`, static tests of the workflow (`tests/unit/test_workflows.py`) and credential-free plan tests of the bootstrap stack.

| Control | How |
| --- | --- |
| No long-lived AWS keys | The workflow assumes `mergeguard-github-deploy` through GitHub's OIDC token. A test fails if an AWS key input or a `secrets.` reference appears in `deploy.yml` |
| Who can assume the role | Audience `sts.amazonaws.com`, and subject `repo:<owner>/<repo>:environment:production-plan` or `…:environment:production`. Nothing else, and no wildcard (asserted in `terraform/bootstrap/tests`) |
| Approval before apply | The apply job runs in the `production` environment, which needs required reviewers. GitHub does not start the job, and so does not issue its token, until a reviewer approves. It applies only the plan that the plan job saved |
| Main only | Both jobs run only on `main`. Restrict both environments to the `main` branch in GitHub |
| Pinned actions | Every third-party action is pinned to a full commit SHA with a version comment, checked by a test |
| Least token | The default `GITHUB_TOKEN` is read-only. `id-token: write` is granted only to the two deploy jobs |
| State | Versioned, encrypted, private S3 bucket that refuses non-TLS requests and cannot be destroyed by Terraform (`prevent_destroy`). State holds no secret values (ADR-0007) |
| Scope of the role | Write actions only on `mergeguard*` resources in one account and region; `iam:PassRole` only to Lambda; no IAM users, access keys or managed policies; state access only under the stack's key prefix |

**Residual risks (TM-16):**

1. **The plan job can also apply.** Both environments use one role. Anyone who can change the workflow on `main` could move an apply into the plan job. Protect `main` with required pull requests and keep both environments main-only. Splitting into a read-only plan role is possible once the first real deploy shows which read permissions Terraform needs.
2. **The role can create IAM roles** named `mergeguard_*`, and put inline policies on them, which is enough to grant itself more than it has. This cannot be removed while Terraform manages the stack's roles. A permissions boundary on the roles it creates would cap it; that is left for a later task.
3. **The role can read the real secret values** of the three SSM parameters, because the AWS provider reads each managed parameter when it refreshes. The values stay out of Terraform state and logs (ADR-0007), but they do pass through the runner's memory.
4. **The first deploy may find a missing permission.** The policy is derived from the Terraform resources. Add what AWS reports as denied; never widen to `*`.
5. **A saved plan is an artifact.** It is kept for one day. It contains resource names and the account ID, never a secret value.

### 5.11 GitHub App and repository configuration (T-601, T-603)

Written in M6, **never run against GitHub**. The signer was checked against OpenSSL; everything else with fakes.

| Control | How |
| --- | --- |
| Key custody | `github_app_private_key` is a fourth SSM SecureString with a Terraform placeholder (ADR-0007). Only the worker role can read it; the receiver cannot. In App mode the personal access token parameter is never read |
| Signing | Pure-Python RS256: blinded private-key operation, signature verified with the public exponent before it is returned, strict DER, 2048 to 8192 bits, no encrypted keys, no private exponent in `repr` ([ADR-0010](ADR/0010-github-app-jwt-signing-without-dependencies.md)) |
| Token scope | One repository per token, only `pull_requests: write`, plus `contents: read` when repository configuration is on and `statuses: write` when the status is on. A permission the App was not granted makes GitHub refuse the token (`422`), which fails closed |
| Token lifetime | At most an hour, cached per execution environment and renewed five minutes early. Never logged |
| Identity | The App's bot account (`<slug>[bot]`) is the author used for the duplicate check and for dismissals, so a copied marker still cannot suppress a review (TM-14) |
| Installation required | In App mode a webhook without an installation ID fails closed (`config.failed`, retried, then dead-lettered). A malformed `installation` object is a `400` |
| Configuration trust | Read from the base commit, validated against operator bounds, all or nothing, and unable to touch the scan or suppressions (ADR-0011, TM-19) |
| Inline comments | Anchored to diff lines only; withheld lines excluded; sanitised; capped (TM-20) |

**Residual risks:** the signer is custom cryptography without a third-party audit; anyone who can read the worker's SSM parameters can read the App key (the deploy role can, §5.10); a repository maintainer can relax their own repository's thresholds within the operator's bounds.

## 6. Secret inventory and rotation

| Secret | Created by | Rotation trigger | Rotation steps |
| --- | --- | --- | --- |
| GitHub token | Operator, in GitHub settings | Before expiry; on suspicion | New token → `aws ssm put-parameter --overwrite` → wait for the cache TTL or redeploy → revoke the old token |
| Webhook secret | Operator (CSPRNG) | Yearly; on suspicion | Update SSM → update GitHub webhook immediately → redeliver any `401` deliveries from the gap |
| Gemini API key | Operator, in Google AI Studio | On suspicion; per owner policy | New key → update SSM → delete the old key in Google AI Studio |
| GitHub App private key | Operator, in the App's settings | On suspicion; per owner policy | Generate a new key in GitHub → update SSM → delete the old key in GitHub (IR-5) |

## 7. Incident runbooks

### IR-1 — MergeGuard GitHub token exposed

1. Revoke the token in GitHub (*Settings → Developer settings → Personal access tokens*).
2. Review recent activity on the covered repositories (PR reviews and comments by the token owner).
3. Issue a new token with the same minimal permissions and store it in SSM.
4. Find how it leaked (logs, fixtures, screenshots) and record the cause.

### IR-2 — Webhook secret exposed

1. Generate a new secret. Update SSM and the GitHub webhook together.
2. Check CloudWatch for unexpected `reviewed` events whose delivery IDs do not appear in GitHub's *Recent Deliveries*.

### IR-3 — Gemini API key exposed

1. Delete the key in Google AI Studio. Create a new one and store it in SSM.
2. Check usage in the Google Cloud console for unexpected traffic.

### IR-4 — MergeGuard reports a credential in a PR

This is the product working as designed; the runbook is for the **PR author**:

1. Revoke or rotate the credential at its provider **immediately**. It is already in the branch history on GitHub.
2. Remove it from the code and load it from SSM or the environment instead.
3. If it reached a default branch, follow the provider's compromise guidance. Rewriting history is not enough on its own.

### IR-5 — GitHub App private key exposed

1. In the App's settings, **generate a new private key**, store it in SSM (`--overwrite`), then **delete the old key** in GitHub. Installation tokens already issued expire within an hour.
2. If you cannot be sure the key was not used, suspend the App's installations until the old key is gone.
3. Review recent reviews, comments and commit statuses by `<slug>[bot]` on the covered repositories.
4. Find how it leaked (logs, a downloaded `.pem`, screenshots) and record the cause. Never keep the downloaded `.pem` file.

## 8. Security review checklist (per release)

- [ ] Signature verification tests pass, including tampered body, wrong prefix, missing header, and base64 body.
- [ ] Scanner corpus passes with no regressions; new rules have positive and negative cases.
- [ ] Log-redaction tests pass; a manual log sample from the E2E run contains no secret, diff, or token.
- [ ] IAM policy has no `*` in actions or resource account/region fields; `checkov` findings are triaged.
- [ ] No new runtime dependency in `src/`.
- [ ] Policy still never emits `APPROVE`.
- [ ] The App key is readable only by the worker role, and a placeholder or unusable key fails closed.
- [ ] Repository configuration is read from the base commit (test `test_the_config_is_read_from_the_base_commit_never_the_head`) and cannot disable the scan.
- [ ] No inline comment is anchored to a line outside the diff or to a withheld line (ST-18).
- [ ] Each Lambda role names only the secrets its function reads (`test_infra_contract.py`); the receiver cannot read the GitHub token.
- [ ] Every suppression is listed in the review, and a suppressed line is absent from the Gemini request body (ST-10, ST-11).
- [ ] The deploy workflow has no AWS keys, pins every action, and its apply job needs the approval environment (`test_workflows.py`).
- [ ] Token permissions and expiry date reviewed.

## 9. Reporting a vulnerability

Do **not** open a public issue for security problems. Use GitHub's private vulnerability reporting for this repository (Proposed: enable it under *Settings → Code security*). If that is unavailable, contact the maintainer [@ChamathDilshanC](https://github.com/ChamathDilshanC) privately through GitHub. Include affected component, reproduction steps, and impact. Never include real secrets in a report.
