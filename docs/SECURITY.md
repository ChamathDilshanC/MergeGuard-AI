# MergeGuard AI — Security

| Field | Value |
| --- | --- |
| Status | Draft v0.1 — security design for implementation; **no controls are implemented yet** |
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
| Terraform state | Internal | Local file (Phase 1) or encrypted S3 (Phase 2) | Contains no secret values by design |

## 3. Trust boundaries

```mermaid
flowchart LR
    subgraph internet["Internet - untrusted"]
        att["Anyone"]
        gh["GitHub"]
    end
    subgraph aws["AWS account - trusted"]
        api["API Gateway"]
        fn["Lambda"]
        ssm["SSM Parameter Store"]
        cw["CloudWatch Logs"]
    end
    subgraph google["Google - third party"]
        gem["Gemini API"]
    end
    att -->|"unsigned or forged requests"| api
    gh -->|"signed webhook"| api
    api --> fn
    fn --> ssm
    fn --> cw
    fn -->|"diff, only after clean scan"| gem
    fn -->|"token-authenticated API calls"| gh
```

- **B1, Internet → API Gateway:** anyone can reach the endpoint. HMAC verification is the trust gate.
- **B2, Lambda → Gemini:** code crosses an organisational boundary. The secret scan gates this crossing ([ADR-0004](ADR/0004-secret-scan-gate-before-ai.md)).
- **B3, Lambda → GitHub:** MergeGuard acts with the token owner's authority. Token scope limits the blast radius.
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
| TM-08 | D | Flooding the public endpoint drives up cost or exhausts concurrency | Invalid signatures rejected quickly; API Gateway stage throttling; optional reserved concurrency; AWS Budgets alert; Gemini only reached after a valid signature | Throttling may delay legitimate deliveries during an attack |
| TM-09 | I | Terraform state or plan files leak | Secrets created outside Terraform; `*.tfstate*` and `tfplan` git-ignored; Phase 2 remote state in encrypted S3 | — |
| TM-10 | T | Supply-chain compromise | Zero runtime dependencies (ADR-0002); providers pinned and `.terraform.lock.hcl` committed; GitHub Actions pinned to commit SHAs (M1 CI) | Dev-only tool compromise (pytest, ruff) |
| TM-11 | T | Model output used for abuse (`@mention` spam, phishing links, oversized body) | Neutralise mentions; cap length; fixed template structure (FR-009) | Misleading prose inside the feedback section |
| TM-12 | R | No trace of why a review was posted | Every log line carries `delivery_id`, repository, PR number, head SHA and decision; GitHub retains reviews | Log retention window (14 days proposed) |
| TM-13 | D | External contributors open many PRs on a public repository and exhaust AI quota | Throttling; AI failures fail open; Phase 3 per-repository config can disable AI for forks | Quota exhaustion degrades reviews to scan-only |

## 5. Controls by component

### 5.1 Webhook verification (FR-002)

- Read the secret from SSM. If it is missing or empty, fail closed.
- Decode the body exactly once: base64 if `isBase64Encoded`, otherwise UTF-8 bytes of `body`. Compute `"sha256=" + HMAC_SHA256(secret, body_bytes).hexdigest()`.
- Compare with `hmac.compare_digest`. Never use `==`. Never short-circuit on header length.
- Do not log the signature header or the body. Log `webhook.rejected` with `delivery_id` only.

### 5.2 GitHub token

- Phase 1: a fine-grained personal access token with the permissions in [API_SPEC.md](API_SPEC.md) §3.3, restricted to the target repositories, with an expiry date.
- Prefer a dedicated machine account over the maintainer's personal account ([OQ-06](PRD.md#17-open-questions)). This also avoids the own-PR `422` limitation.
- Phase 3: GitHub App installation tokens, which are short-lived and scoped per installation ([ADR-0006](ADR/0006-github-identity-pat-then-app.md)).

### 5.3 IAM (FR-012)

Target inline policy for the Phase 1 execution role. ARNs are built in Terraform from data sources, never wildcards:

| Action | Resource |
| --- | --- |
| `logs:CreateLogStream`, `logs:PutLogEvents` | `arn:aws:logs:<region>:<account>:log-group:/aws/lambda/mergeguard-ai-pr-reviewer:*` |
| `ssm:GetParameter`, `ssm:GetParameters` | `arn:aws:ssm:<region>:<account>:parameter/mergeguard/*` |
| `kms:Decrypt` | Only if a customer-managed key replaces `aws/ssm`; scoped to that key ARN |

`logs:CreateLogGroup` is not needed because Terraform creates the log group. The Lambda permission for API Gateway uses `source_arn` scoped to the API's execution ARN.

### 5.4 Secrets management

- Secrets are created with the AWS CLI as `SecureString` ([DEPLOYMENT.md](DEPLOYMENT.md) §3). Keep them out of shell history by reading them interactively, not by typing them inline.
- The webhook secret is random: at least 32 bytes from a CSPRNG, for example `secrets.token_hex(32)`. Do not use memorable strings like the brief's example.
- Lambda caches values per execution environment with a TTL (Proposed: 15 minutes), so a rotation takes effect without a redeploy.

### 5.5 AI data handling

- Only the size-limited diff (file names and patch hunks) is sent. PR title, description, author, and repository metadata are **not** sent in Phase 1.
- Nothing is sent when the secret scan finds anything.
- The owner must check the current Gemini API terms for the chosen tier before reviewing proprietary code. Under Google's published terms, content sent to unpaid tiers may be used to improve Google's products. Tracked as [OQ-05](PRD.md#17-open-questions); see [AI_SYSTEM.md](AI_SYSTEM.md) §3.

### 5.6 Logging and redaction (FR-013)

- Logs are emitted only through `logging_util`, which accepts the allow-listed fields in [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) §7.
- Exceptions are logged by type and a sanitised message. Never log a URL that could carry credentials, or a response body from GitHub or Gemini.

### 5.7 Abuse and cost controls

- API Gateway `$default` stage throttling (Proposed defaults: rate 10 requests/s, burst 20, tunable).
- Optional Lambda reserved concurrency. New AWS accounts can have low concurrency quotas that prevent reserving capacity, so check *Service Quotas* first.
- AWS Budgets alert on the account (Proposed, Phase 2).

### 5.8 Supply chain and repository hygiene

- No runtime dependencies (ADR-0002), enforced by an import check in the test suite. Dev dependencies are pinned in `requirements-dev.txt`, CI tools are pinned in the workflow, and new versions follow a 14-day release cooldown.
- Commit `.terraform.lock.hcl`. Never commit `.terraform/`, `*.tfstate*`, `tfplan`, `lambda_payload.zip`, `.env*`, or credential files.
- Test fixtures use only well-known fake values, for example AWS's documented example key ID `AKIAIOSFODNN7EXAMPLE` ([TESTING_STRATEGY.md](TESTING_STRATEGY.md) §4).

## 6. Secret inventory and rotation

| Secret | Created by | Rotation trigger | Rotation steps |
| --- | --- | --- | --- |
| GitHub token | Operator, in GitHub settings | Before expiry; on suspicion | New token → `aws ssm put-parameter --overwrite` → wait for the cache TTL or redeploy → revoke the old token |
| Webhook secret | Operator (CSPRNG) | Yearly; on suspicion | Update SSM → update GitHub webhook immediately → redeliver any `401` deliveries from the gap |
| Gemini API key | Operator, in Google AI Studio | On suspicion; per owner policy | New key → update SSM → delete the old key in Google AI Studio |

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

## 8. Security review checklist (per release)

- [ ] Signature verification tests pass, including tampered body, wrong prefix, missing header, and base64 body.
- [ ] Scanner corpus passes with no regressions; new rules have positive and negative cases.
- [ ] Log-redaction tests pass; a manual log sample from the E2E run contains no secret, diff, or token.
- [ ] IAM policy has no `*` in actions or resource account/region fields; `checkov` findings are triaged.
- [ ] No new runtime dependency in `src/`.
- [ ] Policy still never emits `APPROVE`.
- [ ] Token permissions and expiry date reviewed.

## 9. Reporting a vulnerability

Do **not** open a public issue for security problems. Use GitHub's private vulnerability reporting for this repository (Proposed: enable it under *Settings → Code security*). If that is unavailable, contact the maintainer [@ChamathDilshanC](https://github.com/ChamathDilshanC) privately through GitHub. Include affected component, reproduction steps, and impact. Never include real secrets in a report.
