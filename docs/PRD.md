# MergeGuard AI — Product Requirements Document

| Field | Value |
| --- | --- |
| Product | MergeGuard AI — serverless DevSecOps pull-request reviewer |
| Repository | [ChamathDilshanC/MergeGuard-AI](https://github.com/ChamathDilshanC/MergeGuard-AI) (private) |
| Owner / maintainer | [@ChamathDilshanC](https://github.com/ChamathDilshanC) |
| Document status | Draft v0.1 — pre-implementation |
| Last updated | 2026-10-21 |
| Related | [Architecture](ARCHITECTURE.md) · [API spec](API_SPEC.md) · [Security](SECURITY.md) · [Tasks](TASKS.md) |

> **Implementation status:** this repository currently contains documentation only. No application code, infrastructure code, or deployment exists yet. Every capability in this document is **Planned (brief)** or **Proposed** — see the [status legend](README.md#status-legend).

---

## 1. Executive summary

MergeGuard AI reviews GitHub pull requests from AWS serverless infrastructure. When a pull request (PR) is opened or updated, GitHub sends a signed webhook to an Amazon API Gateway endpoint. A Python AWS Lambda function verifies the signature, fetches the changed code, and **blocks the PR if hardcoded credentials are detected**. Otherwise it asks Google Gemini for a structured code review and posts the result back to the PR as a GitHub review.

The product is built around four constraints taken from the owner's brief:

1. **No servers to operate** — API Gateway HTTP API + Lambda only.
2. **Zero runtime dependencies** — Python standard library and the `boto3` SDK bundled in the Lambda runtime; no `pip install`, no Lambda layers ([ADR-0002](ADR/0002-zero-runtime-dependencies.md)).
3. **Secrets never in code** — tokens and keys live in AWS SSM Parameter Store as `SecureString` values ([ADR-0003](ADR/0003-secrets-in-ssm-parameter-store.md)).
4. **Reproducible infrastructure** — everything is provisioned with Terraform ([ADR-0001](ADR/0001-serverless-webhook-architecture.md)).

## 2. Source brief and evidence

The owner supplied a brief on 2026-10-21 containing a folder layout, a reference Python Lambda, a reference Terraform file, deployment steps, and a demo scenario. The reference code was **not committed** to this repository; it informs the design but contains defects that the implementation must fix (see [Architecture §16](ARCHITECTURE.md#16-review-of-the-reference-implementation)).

| Brief element | Documented in |
| --- | --- |
| Webhook → Lambda → GitHub/Gemini flow | [ARCHITECTURE.md](ARCHITECTURE.md), [API_SPEC.md](API_SPEC.md) |
| HMAC-SHA256 webhook verification | FR-002, [SECURITY.md](SECURITY.md) |
| Regex secret scanner that blocks the PR | FR-005, FR-006, [ADR-0004](ADR/0004-secret-scan-gate-before-ai.md) |
| Gemini 2.5 Flash JSON review | FR-007, FR-008, [AI_SYSTEM.md](AI_SYSTEM.md) |
| Review event rules (comment vs. request changes) | FR-010 |
| SSM parameters `/mergeguard/*` | FR-011, [DEPLOYMENT.md](DEPLOYMENT.md) |
| Terraform (IAM, Lambda, HTTP API, output URL) | FR-012, [DEPLOYMENT.md](DEPLOYMENT.md) |
| Demo scenario (fake AWS key in a PR) | [TESTING_STRATEGY.md](TESTING_STRATEGY.md) §7 |

## 3. Problem statement

*Assumption — derived from the brief, not yet validated with users.*

- Solo developers and small teams often merge PRs without a dedicated security reviewer. Accidentally committed credentials reach the default branch and get exposed.
- Hosted review tools may need paid seats, and traditional CI-based scanners need runners and maintenance.
- Human reviewers spend time on first-pass checks (obvious bugs, risky patterns, missing error handling) that an automated reviewer could flag first.

## 4. Product vision

> Every pull request gets an immediate, security-first first-pass review, and nobody has to run a server for it.

MergeGuard is **advisory plus a security gate**. It flags issues and can request changes, but a human always makes the merge decision.

## 5. Target users and personas

Primary audience is **unconfirmed** ([OQ-02](#17-open-questions)). Working assumption: the maintainer's own repositories plus small teams who adopt the project.

| ID | Persona | Goals | Interaction with MergeGuard |
| --- | --- | --- | --- |
| P1 | Repository maintainer / operator | Protect the main branch at low cost and effort | Deploys the stack, owns AWS account and tokens, configures the webhook |
| P2 | PR author | Fast, actionable feedback before human review | Reads the review, fixes issues, pushes again |
| P3 | Human reviewer | Spend review time on design, not trivia | Uses the AI summary as a pre-review and still decides on merge |
| P4 | Security-minded lead | No credentials merged; auditable behaviour | Relies on the secret gate, reads logs and review history |

## 6. Goals and non-goals

### 6.1 Goals

| ID | Goal | Indicator |
| --- | --- | --- |
| G1 | Prevent hardcoded credentials from being merged unnoticed | SM-3, SM-4 |
| G2 | Post a first-pass review on every reviewable PR update within about a minute | SM-1 |
| G3 | Zero servers to operate; deployable from a clean AWS account by following one guide | SM-6 |
| G4 | Negligible running cost at small-team scale | SM-5 |
| G5 | Minimal, auditable handling of code and secrets (least privilege, no code persistence) | SM-4, [SECURITY.md](SECURITY.md) |

### 6.2 Non-goals (MVP)

- Replacing human review or making merge decisions. **MergeGuard never sends an `APPROVE` review and never merges** in the MVP. An AI approval could satisfy branch-protection rules and become a prompt-injection target.
- Full SAST/SCA (dependency vulnerability scanning, taint analysis, licence checks).
- GitLab, Bitbucket, or Azure DevOps support.
- A web dashboard, GUI, or user accounts.
- Multi-tenant SaaS or GitHub Marketplace distribution.
- Persisting source code or review history outside GitHub.
- Scanning repository history beyond the PR's changed lines.

## 7. Feature overview — existing vs. planned

No feature is implemented yet. "Planned (brief)" means the owner's brief specifies it; "Proposed" means this documentation recommends it and the owner still has to approve it.

| Feature | Status | Phase | Requirement |
| --- | --- | --- | --- |
| HTTPS webhook endpoint | Planned (brief) | 1 | FR-001 |
| Webhook signature verification | Planned (brief) | 1 | FR-002 |
| Event and action filtering | Planned (brief), extended | 1 | FR-003 |
| Changed-file retrieval with pagination | Planned (brief), extended | 1 | FR-004 |
| Deterministic secret scan | Planned (brief), extended | 1 | FR-005 |
| Secret-leak gate (no AI egress) | Planned (brief) | 1 | FR-006 |
| AI review with structured output | Planned (brief) | 1 | FR-007 |
| AI output validation and fail-open | Proposed | 1 | FR-008 |
| Review rendering and sanitisation | Planned (brief), extended | 1 | FR-009 |
| Review decision policy | Planned (brief), extended | 1 | FR-010 |
| Secrets in SSM Parameter Store | Planned (brief) | 1 | FR-011 |
| Terraform infrastructure | Planned (brief), extended | 1 | FR-012 |
| Structured, redacted logging | Proposed | 1 | FR-013 |
| Duplicate-review prevention | Proposed | 1 | FR-014 |
| Dry-run mode | Proposed | 1 | FR-015 |
| Fast acknowledgement and async processing | Proposed | 2 | FR-016 |
| Commit status check | Proposed | 2 | FR-017 |
| Stale review dismissal | Proposed | 2 | FR-018 |
| Scanner suppressions | Proposed | 2 | FR-019 |
| GitHub App authentication | Proposed | 3 | FR-020 |
| Inline line-level comments | Proposed | 3 | FR-021 |
| Per-repository configuration | Proposed | 3 | FR-022 |

## 8. MVP scope

**In scope (Phase 1):** FR-001 to FR-015.

**Out of scope:** FR-016 to FR-022 and all non-goals in §6.2.

**MVP is done when** the following scenarios pass against a sandbox repository and a deployed stack (see [TESTING_STRATEGY.md](TESTING_STRATEGY.md) §7):

1. A PR that adds a fake AWS access key ID gets a security review within the SM-1 target. Gemini is **not** called, and the review requests changes (or comments, if the PR author owns the bot token — FR-010).
2. A clean PR gets an AI review whose event follows the FR-010 policy.
3. A request with an invalid signature returns `401` and causes no GitHub or Gemini calls.
4. Redelivering the same webhook does not create a second review (FR-014).
5. CloudWatch logs for all scenarios contain no token, secret, diff, or matched credential value (FR-013).

## 9. Functional requirements

Priorities: **P0** = required for MVP, **P1** = should have in its phase, **P2** = could have.

### FR-001 — Webhook endpoint

**Priority:** P0 · **Phase:** 1 · **Status:** Planned (brief)

*Story:* As a maintainer, I want one HTTPS endpoint that GitHub can deliver pull-request events to, so that reviews run without me operating a server.

*Acceptance criteria:*

1. Terraform outputs `github_webhook_url`, ending in `/webhook`.
2. `POST /webhook` invokes the reviewer Lambda through an `AWS_PROXY` integration with payload format `2.0`.
3. Any other method or path returns `404` from API Gateway without invoking the Lambda.

### FR-002 — Webhook signature verification

**Priority:** P0 · **Phase:** 1 · **Status:** Planned (brief)

*Story:* As a maintainer, I want only genuine GitHub deliveries processed, so that nobody can make the bot post reviews or spend my AI quota.

*Acceptance criteria:*

1. The HMAC-SHA256 is computed over the **exact raw request body bytes** (base64-decoded first when API Gateway sets `isBase64Encoded`), keyed by the webhook secret from SSM.
2. The result, formatted `sha256=<hex>`, is compared with `X-Hub-Signature-256` using a constant-time comparison.
3. A missing or invalid signature returns `401`, makes no outbound calls, and logs `webhook.rejected` without the body.
4. Verification happens **before** the JSON is parsed.
5. If the webhook secret cannot be loaded, the function fails closed (`500`, nothing processed).

### FR-003 — Event and action filtering

**Priority:** P0 · **Phase:** 1 · **Status:** Planned (brief), extended

*Story:* As a maintainer, I want the bot to react only to relevant PR activity, so that it never crashes on, or wastes calls for, unrelated events.

*Acceptance criteria:*

1. `X-GitHub-Event: ping` returns `200` with status `pong`.
2. Only `X-GitHub-Event: pull_request` is processed. Every other event returns `200` with status `ignored`.
3. Processed actions: `opened` and `synchronize` (brief), plus `reopened` and `ready_for_review` (Proposed).
4. Draft PRs are skipped until `ready_for_review` (Proposed; [OQ-09](#17-open-questions)).
5. Ignored events still return a `2xx`, so GitHub's delivery log does not show false failures.

### FR-004 — Changed-file retrieval

**Priority:** P0 · **Phase:** 1 · **Status:** Planned (brief), extended

*Story:* As a PR author, I want the review to cover all of my changes, or to say clearly what it skipped.

*Acceptance criteria:*

1. Files come from `GET /repos/{owner}/{repo}/pulls/{pull_number}/files` with `per_page=100`, following pages up to `MERGEGUARD_MAX_FILES` (Proposed default `300`). The brief's code read only the first page of 30 files.
2. Files without a `patch` field (binary or too large) are named in the review as "not reviewed".
3. If the file cap is reached, the review says it is partial.
4. If no file has a patch, no review is posted, and the skip is logged with reason `no_patches` (brief behaviour).
5. Every GitHub request has an explicit timeout (Proposed: 10 s).

### FR-005 — Deterministic secret scan

**Priority:** P0 · **Phase:** 1 · **Status:** Planned (brief), extended

*Story:* As a security-minded lead, I want hardcoded credentials detected reliably and without AI, so that the gate is predictable and testable.

*Acceptance criteria:*

1. Only **added** lines are scanned: lines starting with `+`, excluding the `+++` file header. Removing a secret must never be flagged.
2. Minimum rule set:
   - AWS access key IDs (`AKIA` / `ASIA` prefixes).
   - PEM private-key headers for RSA, EC, DSA, OPENSSH, encrypted, and PKCS#8 keys.
   - GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`).
   - Google API keys (`AIza` prefix).
   - The brief's generic rule: `password`, `secret`, `api_key`, or `token` assigned a quoted literal of 8 or more characters.
3. Each finding records rule ID, file path, and new-file line number. It **never** records the matched value (Proposed: at most a masked hint such as the first four characters plus the length).
4. Rules live in one module, covered by a positive and a negative test corpus ([TESTING_STRATEGY.md](TESTING_STRATEGY.md) §4).
5. The scan completes before any request is sent to Gemini.

### FR-006 — Secret-leak gate

**Priority:** P0 · **Phase:** 1 · **Status:** Planned (brief)

*Story:* As a maintainer, I want a PR containing credentials blocked, with clear remediation steps, and its code kept away from third-party AI.

*Acceptance criteria:*

1. One or more findings produce a `REQUEST_CHANGES` review that uses the security template ([DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) template `T-SEC`). The FR-010 fallback applies.
2. The diff is **not** sent to Gemini.
3. The review tells the author to **revoke or rotate** the credential: it is already exposed in the branch history, so deleting the line is not enough. It should then be loaded from SSM or an environment variable.
4. The review lists rule, file, and line for each finding, and no values.

### FR-007 — AI review request

**Priority:** P0 · **Phase:** 1 · **Status:** Planned (brief)

*Story:* As a PR author, I want an AI first-pass review of bugs, security issues, and code quality, so that I can fix problems before a human looks.

*Acceptance criteria:*

1. The diff goes to the Gemini `generateContent` API. The model comes from `MERGEGUARD_GEMINI_MODEL` (default `gemini-2.5-flash`, per the brief). The API key travels in the `x-goog-api-key` header, **not** the URL query string.
2. JSON output is requested via `responseMimeType: application/json` and a response schema ([AI_SYSTEM.md](AI_SYSTEM.md) §5).
3. The diff is limited to `MERGEGUARD_MAX_DIFF_CHARS` (default `12000`, from the brief) and truncated at file boundaries where possible. The review states the truncation and lists omitted files.
4. The prompt treats the diff as untrusted data ([AI_SYSTEM.md](AI_SYSTEM.md) §7).
5. The Gemini request timeout leaves enough of the Lambda's remaining time to post the review.

### FR-008 — AI output validation and fail-open

**Priority:** P0 · **Phase:** 1 · **Status:** Proposed

*Story:* As a PR author, I do not want my PR blocked because an AI service failed or returned garbage.

*Acceptance criteria:*

1. Validation rules: `risk_level` is one of `LOW`, `MEDIUM`, `HIGH`; `bugs_found` is an integer ≥ 0; `summary` and `feedback_markdown` are strings within length limits.
2. Timeouts, HTTP errors, safety blocks, invalid JSON, or schema mismatches produce a `COMMENT` review from template `T-AIU` stating that the secret scan passed and the AI review is unavailable.
3. AI failure alone never produces `REQUEST_CHANGES`.
4. The failure is logged as `ai.failed` with a reason category and no diff content.

### FR-009 — Review rendering and sanitisation

**Priority:** P0 · **Phase:** 1 · **Status:** Planned (brief), extended

*Acceptance criteria:*

1. Reviews use the templates in [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) §5, including a hidden marker `<!-- mergeguard:v1 ... -->`.
2. Model-supplied text is sanitised: `@mentions` are neutralised so the bot cannot ping people, and output is length-capped.
3. The total review body stays at or below 60,000 characters, under GitHub's 65,536-character limit, with a truncation notice when needed.
4. A footer shows the short head SHA and the AI model ID (when AI was used).

### FR-010 — Review decision policy

**Priority:** P0 · **Phase:** 1 · **Status:** Planned (brief), extended

The policy is evaluated in code, never by the model:

| Condition | Review event | Source |
| --- | --- | --- |
| One or more secret findings | `REQUEST_CHANGES` | Brief |
| AI `risk_level` is `LOW` **and** `bugs_found` is 0 | `COMMENT` | Brief |
| AI `risk_level` is `MEDIUM` or `HIGH`, **or** `bugs_found` > 0 | `REQUEST_CHANGES` | Brief ([OQ-04](#17-open-questions)) |
| AI unavailable or invalid | `COMMENT` | Proposed (FR-008) |
| No reviewable patches | No review | Brief |
| Any case | Never `APPROVE` | Proposed (§6.2) |

*Acceptance criteria:*

1. The table is implemented as a pure function with one unit test per row.
2. If GitHub rejects `REQUEST_CHANGES` with `422` because the token owner authored the PR, the review is retried once as `COMMENT` with a note that changes are requested ([DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) template `T-FALLBACK`).

### FR-011 — Secret configuration via SSM

**Priority:** P0 · **Phase:** 1 · **Status:** Planned (brief)

*Acceptance criteria:*

1. Three `SecureString` parameters exist under the prefix `MERGEGUARD_PARAM_PREFIX` (default `/mergeguard`): `github_token`, `webhook_secret`, `gemini_api_key`.
2. Values are loaded lazily with one `GetParameters` call and cached per execution environment (Proposed: 15-minute TTL so rotation needs no redeploy).
3. Values never appear in environment variables, Terraform code or state, logs, or source.
4. IAM allows reading only these parameter ARNs, in the deployment account and region.

### FR-012 — Infrastructure as code

**Priority:** P0 · **Phase:** 1 · **Status:** Planned (brief), extended

*Acceptance criteria:*

1. Terraform provisions:
   - the IAM role and its inline policy;
   - the Lambda function;
   - a CloudWatch log group with retention (Proposed);
   - the HTTP API, with a `$default` auto-deploy stage, the integration, and the `POST /webhook` route;
   - a Lambda permission scoped to the API;
   - the `github_webhook_url` output.
2. `terraform fmt -check` and `terraform validate` pass.
3. No secret value appears in Terraform code or state.
4. Region and resource names come from variables, with the brief's values as defaults.
5. Stage throttling and optional reserved concurrency are configurable (Proposed; [SECURITY.md](SECURITY.md) TM-08).

### FR-013 — Structured, redacted logging

**Priority:** P0 · **Phase:** 1 · **Status:** Proposed

*Acceptance criteria:*

1. Logs are one JSON object per line, with the fields in [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) §7.
2. Never logged: tokens, the webhook secret, the signature header, the request body, diff or patch content, raw AI output, matched secret values.
3. Log retention is set by a Terraform variable (Proposed default 14 days).

### FR-014 — Duplicate-review prevention

**Priority:** P1 · **Phase:** 1 · **Status:** Proposed

*Acceptance criteria:*

1. Reviews are created with `commit_id` set to the PR head SHA.
2. Before posting, existing PR reviews are listed. If one from the bot identity has the same `commit_id` and a `mergeguard:v1` marker, posting is skipped and the response status is `duplicate`.
3. Redelivering a webhook, or a `reopened` event on an already-reviewed SHA, creates no second review.

### FR-015 — Dry-run mode

**Priority:** P1 · **Phase:** 1 · **Status:** Proposed

*Acceptance criteria:*

1. With `MERGEGUARD_DRY_RUN=true`, the full pipeline runs but nothing is written to GitHub.
2. The decision, finding rule IDs, risk level, and rendered body length are logged. Content is not logged.

### FR-016 — Fast acknowledgement and async processing

**Priority:** P1 · **Phase:** 2 · **Status:** Proposed ([ADR-0005](ADR/0005-async-webhook-processing.md))

*Acceptance criteria:*

1. After verification and filtering, the endpoint returns `202` in under 2 s at p95 — well inside GitHub's 10-second delivery timeout.
2. Queued work holds metadata only (delivery ID, repository, PR number, head SHA, action), never code.
3. Failed work is retried and finally moved to a dead-letter queue. Duplicates are absorbed by FR-014.

### FR-017 — Commit status check

**Priority:** P1 · **Phase:** 2 · **Status:** Proposed

*Acceptance criteria:*

1. A commit status with context `mergeguard/review` is set on the head SHA:
   - `pending` when processing starts;
   - `failure` for secrets or `REQUEST_CHANGES`;
   - `success` for `COMMENT`;
   - `error` for internal failure.
2. The token's required permission (`Commit statuses: write`) is documented in [DEPLOYMENT.md](DEPLOYMENT.md).
3. The owner can mark the status as required in branch protection.

### FR-018 — Stale review dismissal

**Priority:** P1 · **Phase:** 2 · **Status:** Proposed

*Acceptance criteria:*

1. When a newer head SHA ends in `COMMENT`, earlier MergeGuard `REQUEST_CHANGES` reviews on that PR are dismissed with a message naming the new SHA. Without this, an old blocking review keeps the PR blocked after the author fixed the issue.

### FR-019 — Scanner suppressions

**Priority:** P2 · **Phase:** 2 · **Status:** Proposed

*Acceptance criteria:*

1. An inline marker `mergeguard:ignore-secret` on the same line, or a configured path allowlist (for example `tests/fixtures/**`), suppresses a finding.
2. Every suppression is listed in the review for transparency.

### FR-020 — GitHub App authentication

**Priority:** P2 · **Phase:** 3 · **Status:** Proposed ([ADR-0006](ADR/0006-github-identity-pat-then-app.md))

*Acceptance criteria:*

1. The bot authenticates with short-lived installation tokens and appears as `<app-name>[bot]`. This removes the own-PR `422` limitation and enables the Checks API.
2. Multiple repositories are served through one installation.

### FR-021 — Inline line-level comments

**Priority:** P2 · **Phase:** 3 · **Status:** Proposed

*Acceptance criteria:*

1. Secret findings and AI findings that carry a file and line are posted as review comments anchored to the changed line, in addition to the summary.

### FR-022 — Per-repository configuration

**Priority:** P2 · **Phase:** 3 · **Status:** Proposed

*Acceptance criteria:*

1. An optional JSON file in the target repository (`.github/mergeguard.json`, JSON to keep zero dependencies) overrides these settings, within operator-defined bounds:
   - blocking thresholds;
   - ignored paths;
   - comment language.

## 10. Roles and permission boundaries

| Actor | System | Can | Cannot |
| --- | --- | --- | --- |
| Repository admin (P1) | GitHub | Create the webhook and set its secret; choose events | Read SSM values without AWS access |
| PR author (P2) | GitHub | Trigger reviews by opening or pushing to PRs | Change the policy; diff content is untrusted input |
| Human reviewer (P3) | GitHub | Approve, merge, dismiss MergeGuard reviews | — |
| Bot identity (PAT owner, later a GitHub App) | GitHub API | Read PR files; create reviews; later, set commit statuses and dismiss its own reviews | Push code, merge, administer the repository (must not be granted) |
| AWS operator (P1) | AWS | Deploy with Terraform; create and rotate SSM parameters; read logs | — |
| Lambda execution role | AWS | Write to its own log group; read the three `/mergeguard/*` parameters | Anything else |
| Gemini API | Google | Receives diffs **only** when the secret scan is clean | Receives secrets, tokens, or PR metadata beyond the diff |

## 11. Workflows and edge cases

### 11.1 Primary workflow

1. The author opens a PR or pushes a commit to one.
2. GitHub delivers a `pull_request` webhook to `POST /webhook`.
3. The Lambda verifies the signature and filters the event (FR-002, FR-003).
4. It fetches the changed files (FR-004) and scans the added lines for secrets (FR-005).
5. If secrets are found, it posts the security review and stops (FR-006).
6. Otherwise it requests the AI review (FR-007), validates the output (FR-008), and applies the policy (FR-010).
7. It posts the review (FR-009) unless this head SHA already has one (FR-014).
8. The author fixes issues and pushes; the flow repeats for the new head SHA.

### 11.2 Edge cases

| ID | Case | Expected behaviour |
| --- | --- | --- |
| EC-01 | GitHub `ping` after webhook creation | `200 pong`, no outbound calls |
| EC-02 | PR from a fork | Processed like any PR; diff treated as untrusted (prompt injection, [SECURITY.md](SECURITY.md) TM-03) |
| EC-03 | Draft PR | Skipped until `ready_for_review` (Proposed) |
| EC-04 | Very large PR (more than `MERGEGUARD_MAX_FILES` files or `MERGEGUARD_MAX_DIFF_CHARS` characters) | Partial review with explicit notice; secret scan still covers every retrieved patch |
| EC-05 | Binary-only or patch-less changes | No review; logged `no_patches` |
| EC-06 | Force-push (`synchronize` with rewritten history) | Reviewed as the new head SHA |
| EC-07 | Rapid successive pushes | Each SHA is reviewed; GitHub marks reviews on older SHAs as outdated. Phase 2 checks that the head is still current before posting |
| EC-08 | PR authored by the bot token's owner | `REQUEST_CHANGES` rejected with `422`; retried as `COMMENT` (FR-010) |
| EC-09 | PR that removes a previously committed secret | Not flagged; only added lines are scanned |
| EC-10 | Gemini `429` or quota exhaustion | AI-unavailable comment (FR-008) |
| EC-11 | GitHub API rate limit (`403`/`429`) | Logged; `500` returned so the delivery shows as failed and can be redelivered |
| EC-12 | Webhook secret changed in GitHub but not in SSM, or the reverse | All deliveries `401` until both match ([DEPLOYMENT.md](DEPLOYMENT.md) runbook) |
| EC-13 | Expired or revoked GitHub token | GitHub returns `401`; logged as `github.auth_failed`; `500` response |
| EC-14 | PR closed between event and processing | Review is still attempted; a GitHub error is logged and not retried |
| EC-15 | Webhook redelivered by an operator | Duplicate suppressed (FR-014) |

## 12. Non-functional requirements

All targets are **Proposed** until the owner confirms them.

| ID | Category | Requirement | Proposed target | Verification |
| --- | --- | --- | --- | --- |
| NFR-001 | Performance | Time from webhook receipt to review posted | p95 ≤ 60 s for diffs within the size budget | CloudWatch duration metrics, E2E test |
| NFR-002 | Performance | Webhook acknowledgement | Phase 1: best effort (synchronous). Phase 2: p95 < 2 s | GitHub delivery log, API Gateway latency metric |
| NFR-003 | Reliability | Successful delivery rate (2xx) for valid events | ≥ 99% after Phase 2 | GitHub Recent Deliveries, Lambda error metric |
| NFR-004 | Security | Least privilege for the Lambda role and the GitHub token | No wildcard account or region in IAM ARNs; token limited to selected repositories | Terraform review, checkov ([TESTING_STRATEGY.md](TESTING_STRATEGY.md) §5) |
| NFR-005 | Privacy | No code persisted by MergeGuard; diffs with secret findings never leave AWS | 100% of secret-positive test cases make zero Gemini calls | Unit and integration tests |
| NFR-006 | Privacy | No sensitive values in logs or comments | Zero occurrences in the redaction test suite | Log-redaction tests |
| NFR-007 | Cost | Running cost at small-team scale | Within the owner's budget (TBD, [OQ-05](#17-open-questions)); AWS Budgets alert configured | AWS Cost Explorer |
| NFR-008 | Maintainability | Zero runtime dependencies beyond stdlib and `boto3` | Lambda package contains first-party code only | Package inspection in CI |
| NFR-009 | Maintainability | Automated test coverage of `src/` | ≥ 85% line coverage | `pytest --cov` in CI |
| NFR-010 | Scalability | Concurrent PR events | Handles bursts of parallel deliveries; cost bounded by throttling and optional reserved concurrency | Load test (Phase 2) |
| NFR-011 | Accessibility | Review comments readable by assistive technology | Text labels with every emoji; ordered headings; tables at most 4 columns | Template checklist ([DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) §9) |
| NFR-012 | Portability | Region and names configurable | Deployable outside `us-east-1` by changing one variable | Terraform `plan` in a second region |

## 13. Success indicators

| ID | Indicator | Proposed target |
| --- | --- | --- |
| SM-1 | p95 webhook-to-review latency | ≤ 60 s |
| SM-2 | Valid deliveries acknowledged within GitHub's 10 s timeout | ≥ 99% (Phase 2) |
| SM-3 | Secret scanner recall on the positive corpus / false-positive rate on the negative corpus | 100% / ≤ 5% |
| SM-4 | Sensitive values found in logs or comments during review | 0 |
| SM-5 | Monthly AWS + Gemini cost at expected volume | ≤ owner-defined budget (TBD) |
| SM-6 | Time to deploy from a clean AWS account by following [DEPLOYMENT.md](DEPLOYMENT.md) | ≤ 30 minutes |
| SM-7 | Share of AI reviews that authors rate useful (manual sampling) | Tracked qualitatively from Phase 1 |

## 14. Dependencies

| Dependency | Used for | Notes |
| --- | --- | --- |
| GitHub webhooks and REST API | Events in; files and reviews out | Fine-grained PAT in Phase 1 |
| Google Gemini API (Generative Language API) | AI review | Model and terms may change; [AI_SYSTEM.md](AI_SYSTEM.md) §11 |
| AWS API Gateway (HTTP API), Lambda, SSM Parameter Store, CloudWatch Logs, IAM | Runtime platform | Single account and region in Phase 1 |
| Terraform with `hashicorp/aws` and `hashicorp/archive` providers | Provisioning | Brief pins AWS provider `~> 5.0`; see [OQ-07](#17-open-questions) |
| Python 3.12 Lambda runtime (bundles `boto3`) | Execution | Local development must also target 3.12 |

## 15. Assumptions

| ID | Assumption |
| --- | --- |
| A-01 | One AWS account and region (`us-east-1`, per the brief) for Phase 1 |
| A-02 | A small number of repositories owned by the maintainer, each with a repository-level webhook |
| A-03 | The bot uses a fine-grained personal access token in Phase 1 |
| A-04 | Most PR diffs fit within 12,000 characters; larger ones get a partial AI review |
| A-05 | Review comments are written in English ([OQ-03](#17-open-questions)) |
| A-06 | The Python 3.12 Lambda runtime continues to bundle `boto3` |
| A-07 | The chosen Gemini model supports JSON output mode and response schemas |

## 16. Risks

| ID | Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- | --- |
| R-01 | Synchronous AI call exceeds GitHub's 10 s webhook timeout | High | Medium — deliveries show as failed; a review may still post | Phase 2 async processing (FR-016, ADR-0005) |
| R-02 | Prompt injection in a diff manipulates the AI verdict | Medium | Medium — a misleading "LOW" review | Policy in code, no `APPROVE`, untrusted-data prompting, human merge decision ([SECURITY.md](SECURITY.md) TM-03) |
| R-03 | Noisy or wrong AI feedback erodes trust | Medium | Medium | Evaluation set, tuned prompt, OQ-04 threshold decision |
| R-04 | Proprietary code sent to an AI provider under unsuitable data terms | Medium | High | Secret gate first; owner decides on tier ([OQ-05](#17-open-questions)); documented in [AI_SYSTEM.md](AI_SYSTEM.md) §3 |
| R-05 | Over-privileged or expired GitHub token | Medium | High / Medium | Fine-grained token, minimal permissions, expiry tracking, rotation runbook |
| R-06 | Own-PR `422` when the token owner is the author (typical for demos) | High | Low | FR-010 fallback; dedicated bot identity or GitHub App (ADR-0006) |
| R-07 | Cost abuse via the public endpoint | Low | Medium | Cheap `401` path, API throttling, AWS Budgets alert |
| R-08 | AI model deprecation or behaviour change | Medium | Medium | Model ID in configuration; re-run evaluation on change |
| R-09 | Regex false negatives give a false sense of security | Medium | High | Document scope honestly; complement with GitHub secret scanning and push protection where available |

## 17. Open questions

| ID | Question | Needed by |
| --- | --- | --- |
| OQ-01 | **Resolved 2026-10-21:** product name is MergeGuard AI; repository `ChamathDilshanC/MergeGuard-AI`, private | M1 |
| OQ-02 | Who is the primary audience: personal or portfolio use, small teams, or organisation-wide? | M1 |
| OQ-03 | Comment language: English only, or optional Sinhala as in the brief's alert text? | M2 |
| OQ-04 | Should AI `MEDIUM` risk request changes, or only `HIGH`? | M3 |
| OQ-05 | Gemini free or paid tier, given data-use terms and rate limits? Monthly cost budget? | M3 |
| OQ-06 | Bot identity for Phase 1: personal token or a dedicated machine account? | M4 |
| OQ-07 | AWS provider major version: keep `~> 5.0` (brief) or adopt the current major? | M1 |
| OQ-08 | Terraform state: local for the demo, or a remote S3 backend from the start? | M1 |
| OQ-09 | Review draft PRs or skip them until ready? | M2 |
| OQ-10 | Deployment region: keep `us-east-1`? | M4 |
| OQ-11 | Licence for the repository (none present)? | M1 |

## 18. Roadmap

| Phase | Goal | Scope | Exit criteria |
| --- | --- | --- | --- |
| 0 — Documentation | Shared understanding before code | This documentation suite | Owner reviews docs and answers M1 open questions |
| 1 — MVP | Brief parity with its defects fixed | FR-001 to FR-015; milestones M1–M4 in [TASKS.md](TASKS.md) | §8 scenarios pass on a deployed stack |
| 2 — Reliability and signal | Fast acknowledgement, mergeable status, less noise | FR-016 to FR-019, remote state, CI/CD deploy | SM-2 met; stale reviews no longer block |
| 3 — Distribution | Multi-repo use with a proper bot identity | FR-020 to FR-022 | Installed on at least two repositories via a GitHub App |

## 19. Traceability

| Requirement | Architecture | ADR | Tasks | Tests |
| --- | --- | --- | --- | --- |
| FR-001 | §5 API Gateway | 0001 | T-201, T-401 | Terraform validate, E2E |
| FR-002 | §5 webhook module, §9 | 0001 | T-202 | Unit: signature suite |
| FR-003 | §5 webhook module | — | T-201 | Unit: event filter |
| FR-004 | §5 GitHub client | — | T-204 | Unit + contract |
| FR-005 | §5 scanner | 0004 | T-205 | Scanner corpus |
| FR-006 | §6 sequence | 0004 | T-207 | Integration: no AI call |
| FR-007 | §5 AI reviewer | 0002 | T-301, T-302 | Contract: Gemini fixtures |
| FR-008 | §11 | — | T-303 | Unit: invalid outputs |
| FR-009 | §5 formatter | — | T-206 | Snapshot tests |
| FR-010 | §5 policy | — | T-207 | Unit: policy table |
| FR-011 | §9, §10 | 0003 | T-203 | Unit: config loader |
| FR-012 | §14 | 0001 | T-401 | fmt, validate, checkov |
| FR-013 | §12 | — | T-208 | Log-redaction tests |
| FR-014 | §11 | — | T-209 | Integration: redelivery |
| FR-015 | §10 | — | T-210 | Integration: dry run |
| FR-016 | §15 | 0005 | T-501 | Integration + load |
| FR-017 | §15 | — | T-502 | Contract |
| FR-018 | §15 | — | T-503 | Contract |
| FR-019 | §5 scanner | 0004 | T-504 | Scanner corpus |
| FR-020 | §15 | 0006 | T-601 | Contract |
| FR-021 | §15 | — | T-602 | Contract |
| FR-022 | §15 | 0002 | T-603 | Unit |
