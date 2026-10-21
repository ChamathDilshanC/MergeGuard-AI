# MergeGuard AI — Engineering Backlog

| Field | Value |
| --- | --- |
| Status | Living document — update the status column in the same PR that changes a task |
| Last updated | 2026-10-21 |
| Related | [PRD](PRD.md) §18 roadmap · [Architecture](ARCHITECTURE.md) §16 (`RB-` defects) |

**Status values:** `Done`, `In progress`, `Not started`, `Blocked`.

## Milestone overview

| Milestone | Phase | Goal | Depends on |
| --- | --- | --- | --- |
| M0 — Documentation baseline | 0 | Agreed requirements and design | — |
| M1 — Foundation | 1 | Repository scaffold, tooling, CI, key decisions | M0 |
| M2 — Core pipeline (no AI) | 1 | Signed webhook → files → secret gate → review | M1 |
| M3 — AI review | 1 | Gemini review with validation and fail-open | M2 |
| M4 — Infrastructure and demo | 1 | Terraform, sandbox deployment, E2E pass (MVP exit) | M2, M3 |
| M5 — Reliability and signal | 2 | Async processing, commit status, stale-review dismissal, suppressions, CI deploy, alarms | M4 |
| M6 — Distribution | 3 | GitHub App, inline comments, per-repository config | M5 |

**Recommended next step:** T-402 and T-403 (the first real deployment and the end-to-end run), after the open questions listed in the M4 and M5 notes are answered. M1 to M3 proved the riskiest guarantees (signature verification, secret detection, no-AI-on-secrets, log redaction) with tests; M4 and M5 are written but untested against AWS.

## M0 — Documentation baseline

| ID | Task | Requirements | Acceptance criteria | Status |
| --- | --- | --- | --- | --- |
| T-001 | Create the project documentation suite | All | PRD, architecture, design system, API, security, AI, testing, deployment, ADRs, agent and contributor guides exist and cross-reference each other | Done |
| T-002 | Owner review of docs; answer M1 open questions (OQ-02, 07, 08, 11; OQ-01 resolved) | — | Answers recorded in [PRD.md](PRD.md) §17; affected docs updated | Not started |

## M1 — Foundation

| ID | Task | Requirements | Depends on | Acceptance criteria | Status |
| --- | --- | --- | --- | --- | --- |
| T-101 | Scaffold layout from [ARCHITECTURE.md](ARCHITECTURE.md) §4.2 and add `.gitignore` | RB-17 | T-002 | `.gitignore` covers `.terraform/`, `*.tfstate*`, `tfplan`, `*.zip`, `terraform.tfvars`, `__pycache__/`, `.venv/`, `.env*`, `tests/ai_eval/results/` | Done |
| T-102 | Dev tooling: Python 3.12 pin, `requirements-dev.txt` (`pytest`, `pytest-cov`, `ruff`), `pyproject.toml` config | NFR-008, RB-01 | T-101 | `ruff check`, `ruff format --check` and `pytest` run locally; commands in [AGENTS.md](../AGENTS.md) §6 change from Proposed to Verified | Done |
| T-103 | Terraform skeleton: `versions.tf` and `providers.tf` with pinned versions, `variables.tf` from [DEPLOYMENT.md](DEPLOYMENT.md) §4 | FR-012, OQ-07 | T-101 | `terraform validate` passes; `.terraform.lock.hcl` committed | Done (constraint `~> 5.0` from brief; OQ-07 still open) |
| T-104 | CI workflow `ci.yml`: lint, tests on 3.12, Terraform fmt/validate, checkov | §9 quality gates in [TESTING_STRATEGY.md](TESTING_STRATEGY.md) | T-102, T-103 | Workflow green on a PR; actions pinned to SHAs | Done (green on PRs #1 and #2 and on `main`) |

**M1 notes:**

- M1 started before T-002 (owner review), at the maintainer's request. OQ-02, OQ-07, OQ-08 and OQ-11 remain open; none blocked the skeleton.
- T-101 created the package (`src/mergeguard/__init__.py`) and the test directories only. Each feature module in §4.2 is created by the task that implements it, so no placeholder modules exist.

## M2 — Core pipeline (no AI)

| ID | Task | Requirements | Depends on | Acceptance criteria | Status |
| --- | --- | --- | --- | --- | --- |
| T-201 | Handler and event/action filtering | FR-001, FR-003, RB-04 | T-102 | Unit tests for ping, non-PR events, each action, drafts; API responses match [API_SPEC.md](API_SPEC.md) §2.3 | Done |
| T-202 | Signature verification incl. base64 bodies | FR-002, RB-14 | T-201 | ST-01 to ST-04 pass | Done |
| T-203 | Config loader: env vars + cached `GetParameters` with TTL | FR-011, RB-13 | T-102 | Fail-closed on missing parameter; TTL unit-tested | Done |
| T-204 | HTTP transport + GitHub client (files pagination, reviews, timeouts, error mapping) | FR-004, RB-06, RB-08 | T-102 | Contract tests for page 1/2, no-patch files, each error class in [API_SPEC.md](API_SPEC.md) §3.2 | Done |
| T-205 | Secret scanner over added lines with corpus | FR-005, RB-02, RB-03 | T-102 | Corpus gates in [TESTING_STRATEGY.md](TESTING_STRATEGY.md) §4 pass; rule IDs match [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) §8 | Done |
| T-206 | Renderer: templates, notices, sanitisation, length caps | FR-009, RB-15, RB-16 | T-102 | Snapshot tests for every template and notice; ST-07 passes | Done (except `N-SUPPRESSED`, moved to T-504) |
| T-207 | Decision policy, leak gate and `422` fallback | FR-006, FR-010, RB-01, RB-10 | T-204, T-205, T-206 | ST-05 and ST-09 pass; fallback contract test passes | Done |
| T-208 | Structured logging with allow-listed fields | FR-013 | T-201 | ST-06 passes | Done |
| T-209 | Duplicate-review prevention | FR-014, RB-12 | T-204, T-206 | Redelivery integration test produces exactly one review | Done |
| T-210 | Dry-run mode | FR-015 | T-207 | No GitHub write calls when enabled (asserted on fake transport) | Done |

**M2 notes:**

- **No AI step yet.** With a clean scan, M2 posted nothing and answered `skipped` / `ai_not_enabled`. M3 replaced this with the AI review.
- **Duplicate check verifies the reviewer identity** (`GET /user`) as well as the marker and SHA, so a copied marker cannot suppress a security review ([SECURITY.md](SECURITY.md) TM-14).
- **POSTs are not retried on `5xx`,** because the review may already exist ([API_SPEC.md](API_SPEC.md) §3.2).
- **Open questions:** draft PRs are skipped (the documented proposal for OQ-09, a single constant in `webhook.py`). Comments are English only (OQ-03). AI `MEDIUM` requests changes, as in the brief (OQ-04, exercised from M3). None of these blocked M2, and none is settled.
- **Self-scan:** a test scans this repository with the scanner. It found and fixed one false positive class: values like `"{name}"` (format templates).

## M3 — AI review

| ID | Task | Requirements | Depends on | Acceptance criteria | Status |
| --- | --- | --- | --- | --- | --- |
| T-301 | Gemini client with header auth and deadline-based timeout | FR-007, RB-07, RB-08 | T-204 | Contract tests with recorded responses; key never in URL (asserted) | Done |
| T-302 | Prompt v1, boundary markers, response schema, diff truncation | FR-007, RB-18 | T-301 | ST-08 passes; truncation unit tests; `N-PARTIAL-DIFF` rendered | Done |
| T-303 | Output validation and fail-open path | FR-008, RB-09 | T-302 | Each failure row in [AI_SYSTEM.md](AI_SYSTEM.md) §8 has a test that yields `T-AIU` | Done |
| T-304 | AI evaluation set and runner | [AI_SYSTEM.md](AI_SYSTEM.md) §9 | T-302 | About 30 labelled cases; first run meets gates or the gaps are documented; OQ-04 and OQ-05 answered | Blocked (needs real Gemini calls; OQ-05) |

**M3 notes:**

- **Mocked only.** No real Gemini call was made, because the tier is undecided (OQ-05). All tests use mocked or recorded responses.
- **OQ-04 not settled.** The documented default is in a named constant (`policy.COMMENT_ONLY_RISK_LEVELS`): only `LOW` with 0 bugs is a plain comment.
- **Fail-safe.** Every Gemini failure gives the `T-AIU` comment and never `REQUEST_CHANGES`. If the policy ever gets neither a review nor a failure reason, it still comments (`unavailable`) rather than staying silent. The M2 `ai_not_enabled` skip is gone.
- **Gemini key.** It is read from SSM in its own call, only when an AI review runs, so a missing key cannot break the secret gate.
- **Two new failure reasons:** `not_configured` (missing key) and `diff_too_large` (nothing fits the budget). See [AI_SYSTEM.md](AI_SYSTEM.md) §8.

## M4 — Infrastructure and demo (MVP exit)

| ID | Task | Requirements | Depends on | Acceptance criteria | Status |
| --- | --- | --- | --- | --- | --- |
| T-401 | Terraform resources: scoped IAM, Lambda (`source_dir`), log group with retention, HTTP API, throttling, output | FR-012, RB-17 | T-103 | `validate` + checkov pass; IAM matches [SECURITY.md](SECURITY.md) §5.3 | Done (merged in PR #4; CI green on `9526b9a`: checkov, `terraform test`, validate). Extended in M5 (T-501, T-506) |
| T-402 | Deploy to sandbox using [DEPLOYMENT.md](DEPLOYMENT.md) exactly; fix doc gaps found | SM-6 | T-401, M2, M3 | Fresh deploy completes within SM-6 target; doc updated | Not started |
| T-403 | Run E2E-1 to E2E-9 | PRD §8 | T-402 | All pass; results recorded in the release PR | Not started |
| T-404 | Update doc statuses from Planned/Proposed to Verified where implemented | — | T-403 | Docs match shipped behaviour | Not started |

**M4 notes (T-401):**

- **Written, not applied.** The Terraform stack exists; no AWS credentials were used and nothing was deployed. T-402 and T-403 need a real AWS account.
- **Credential-free plan.** `terraform -chdir=terraform test` plans with a mocked AWS provider: 13 resources to add, and 4 runs asserting the SECURITY.md §5.3 policy, throttling, route, retention and SSM placeholders. It now runs in CI.
- **SSM placeholders (ADR-0007).** Values are written with the write-only `value_wo`, so they never enter state, and the Lambda treats the placeholder as not set. Before the real Gemini key is stored, no diff can reach Gemini. Terraform must be 1.11 or later.
- **checkov.** It could not be installed on the development machine at the time, so the expected findings were derived from checkov 3.3.26's own source. 11 check IDs are skipped inline, with reasons ([SECURITY.md](SECURITY.md) §5.9); access logging was fixed instead. CI then ran checkov for the first time: one false positive (its secrets scan flagged a Terraform local named like a credential) was fixed by renaming the local, and the next run was green.
- **Tighter than the brief:** the Lambda permission allows only `POST /webhook`, and API Gateway writes access logs to a second log group.
- **Open questions to settle before T-402:** OQ-05 (Gemini tier; keep the Gemini key as the placeholder until then), OQ-06 (bot identity), OQ-08 (state backend; local state for now), OQ-10 (region). OQ-07 (AWS provider major) is unchanged. None is settled here.

## M5 — Reliability and signal (Phase 2)

| ID | Task | Requirements | Acceptance criteria | Status |
| --- | --- | --- | --- | --- |
| T-501 | Receiver/worker split with SQS and DLQ | FR-016, ADR-0005, RB-05 | p95 acknowledgement < 2 s; DLQ alarm; duplicates absorbed | In review (code, Terraform and tests pass; duplicates absorbed and the DLQ alarm defined. **p95 acknowledgement is not measured**: it needs a deployment, T-403) |
| T-502 | Commit status `mergeguard/review` | FR-017 | States per [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) §5.6; usable as a required check | In review (states and texts tested; **"usable as a required check" is not confirmed**, it needs a real repository, T-403) |
| T-503 | Dismiss stale MergeGuard `REQUEST_CHANGES` reviews | FR-018, RB-11 | Fixing a flagged PR leaves no active blocking MergeGuard review | In review (tested with a fake GitHub; **GitHub's behaviour on protected branches is not confirmed**, T-403) |
| T-504 | Scanner suppressions, including the `N-SUPPRESSED` notice | FR-019 | Inline marker and path allow-list tested; suppressions listed in the review | In review (all acceptance criteria met by tests; see [ADR-0008](ADR/0008-scanner-suppressions-are-visible-and-withheld-from-ai.md)) |
| T-505 | Remote Terraform state and OIDC-based deploy workflow | [DEPLOYMENT.md](DEPLOYMENT.md) §8 | Deploy from CI with approval; no static AWS keys in GitHub | In review (workflow and bootstrap stack written and statically checked; **the workflow and the bootstrap stack have never run**, and the deploy role's permissions are unconfirmed until T-402) |
| T-506 | Alarms and AWS Budgets alert | NFR-007, TM-08 | Alarms fire in a test; budget alert configured | In review (alarms and budget defined and plan-tested; **an alarm has never fired**, and no budget exists until the owner sets `monthly_budget_usd`) |

**M5 notes:**

- **What is verified.** 553 unit and integration tests pass with fakes for GitHub, SSM and SQS and a mocked Gemini (no real call, OQ-05). `terraform test` covers the stack (12 plan runs) and the bootstrap stack (7 runs) with a mocked AWS provider. No AWS credentials were used, nothing was applied, and the deploy workflow was not run.
- **checkov** ran in CI on PR #5: 78 passed, 0 failed, 33 skipped, with the skips and reasons in [SECURITY.md](SECURITY.md) §5.9. It first reported a parsing error for `terraform/variables.tf` (it would have skipped the file silently); that was fixed and CI now fails on any parsing error.
- **What is not verified.** Anything that needs a deployment: acknowledgement latency, alarms firing, SQS retry and redrive behaviour, event source mapping behaviour, GitHub's handling of commit statuses and dismissals, the deploy role's permissions, and the OIDC trust. These move to T-402 and T-403 ([TESTING_STRATEGY.md](TESTING_STRATEGY.md) E2E-10 to E2E-13).
- **One code package, two handlers.** `lambda_function.lambda_handler` is the receiver and `worker_function.lambda_handler` the worker, with separate IAM roles: the receiver reads only the webhook secret, the worker only the token and the Gemini key. With `MERGEGUARD_QUEUE_URL` unset the handler still reviews inline, which the tests and local runs use; the Terraform stack always sets it.
- **Failure model.** The worker reports a failed record instead of crashing, so SQS retries it three times and then dead-letters it. That is why the alarms watch the dead-letter queue and API Gateway `5xx`: Lambda `Errors` would stay at zero for those failures.
- **Signals never stop a review.** A permanent failure to set a commit status or dismiss a review (missing permission, protected branch) is logged and the review stands. A transient one is retried, and a retry that finds its own review restores the final status and the dismissals from it.
- **Suppressions** are listed in every review and withheld from the AI (ADR-0008). The allow-list is operator-only. A real credential marked with the inline marker is not blocked; that residual risk is TM-15.
- **Remote state is opt-in** (ADR-0009): CI selects the S3 backend through a generated override file, and local state stays the default. **OQ-08 is not settled.** The deploy workflow is manual only; no push trigger is enabled.
- **Known limits.** A new head with no reviewable patch posts no review and dismisses nothing. After an *AI review unavailable* comment, an earlier AI-based block is kept and only earlier secret blocks are dismissed (owner's decision; it departs from FR-018's literal wording). The status is still `success`, because nothing blocks the new commit. The plan job holds a role that can also apply (TM-16). The first real deploy will probably need adjustments to the deploy role.
- **Doc-only decisions to confirm.** ADR-0005 changed from Proposed to Accepted because M5 implements it. ADR-0008 and ADR-0009 are new. FR-016 to FR-019 changed from Proposed to Verified in code and tests.
- **Open questions to settle before T-402** (none is settled here): OQ-05 (Gemini tier, and the monthly budget amount; keep the Gemini key as the placeholder until then), OQ-06 (bot identity: statuses and dismissals also depend on whose token it is), OQ-08 (local or remote state), OQ-10 (region). OQ-07 (AWS provider major) is unchanged. The owner also has to choose `alert_email` and `monthly_budget_usd`.

## M6 — Distribution (Phase 3)

| ID | Task | Requirements | Acceptance criteria | Status |
| --- | --- | --- | --- | --- |
| T-601 | GitHub App authentication (new ADR for JWT signing) | FR-020, ADR-0006 | Reviews appear as `[bot]`; works on two or more repositories | Not started |
| T-602 | Inline line-level comments | FR-021 | Comments anchored only to lines present in the diff | Not started |
| T-603 | Per-repository JSON config | FR-022 | Bounds enforced; invalid config falls back to defaults with a notice | Not started |
