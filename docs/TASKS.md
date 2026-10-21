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
| M5 — Reliability and signal | 2 | Async processing, commit status, stale-review dismissal | M4 |
| M6 — Distribution | 3 | GitHub App, inline comments, per-repository config | M5 |

**Recommended next step:** M1, then M2. M2 delivers the deterministic security gate before any AI integration, so the riskiest guarantees (signature verification, secret detection, no-AI-on-secrets, log redaction) are proven first.

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

- **No AI step yet.** With a clean scan, M2 posts nothing and answers `skipped` / `ai_not_enabled` ([API_SPEC.md](API_SPEC.md) §2.3). M3 inserts the AI review between the scan and the decision.
- **Duplicate check verifies the reviewer identity** (`GET /user`) as well as the marker and SHA, so a copied marker cannot suppress a security review ([SECURITY.md](SECURITY.md) TM-14).
- **POSTs are not retried on `5xx`,** because the review may already exist ([API_SPEC.md](API_SPEC.md) §3.2).
- **Open questions:** draft PRs are skipped (the documented proposal for OQ-09, a single constant in `webhook.py`). Comments are English only (OQ-03). AI `MEDIUM` requests changes, as in the brief (OQ-04, exercised from M3). None of these blocked M2, and none is settled.
- **Self-scan:** a test scans this repository with the scanner. It found and fixed one false positive class: values like `"{name}"` (format templates).

## M3 — AI review

| ID | Task | Requirements | Depends on | Acceptance criteria | Status |
| --- | --- | --- | --- | --- | --- |
| T-301 | Gemini client with header auth and deadline-based timeout | FR-007, RB-07, RB-08 | T-204 | Contract tests with recorded responses; key never in URL (asserted) | Not started |
| T-302 | Prompt v1, boundary markers, response schema, diff truncation | FR-007, RB-18 | T-301 | ST-08 passes; truncation unit tests; `N-PARTIAL-DIFF` rendered | Not started |
| T-303 | Output validation and fail-open path | FR-008, RB-09 | T-302 | Each failure row in [AI_SYSTEM.md](AI_SYSTEM.md) §8 has a test that yields `T-AIU` | Not started |
| T-304 | AI evaluation set and runner | [AI_SYSTEM.md](AI_SYSTEM.md) §9 | T-302 | About 30 labelled cases; first run meets gates or the gaps are documented; OQ-04 and OQ-05 answered | Not started |

## M4 — Infrastructure and demo (MVP exit)

| ID | Task | Requirements | Depends on | Acceptance criteria | Status |
| --- | --- | --- | --- | --- | --- |
| T-401 | Terraform resources: scoped IAM, Lambda (`source_dir`), log group with retention, HTTP API, throttling, output | FR-012, RB-17 | T-103 | `validate` + checkov pass; IAM matches [SECURITY.md](SECURITY.md) §5.3 | Not started |
| T-402 | Deploy to sandbox using [DEPLOYMENT.md](DEPLOYMENT.md) exactly; fix doc gaps found | SM-6 | T-401, M2, M3 | Fresh deploy completes within SM-6 target; doc updated | Not started |
| T-403 | Run E2E-1 to E2E-9 | PRD §8 | T-402 | All pass; results recorded in the release PR | Not started |
| T-404 | Update doc statuses from Planned/Proposed to Verified where implemented | — | T-403 | Docs match shipped behaviour | Not started |

## M5 — Reliability and signal (Phase 2)

| ID | Task | Requirements | Acceptance criteria | Status |
| --- | --- | --- | --- | --- |
| T-501 | Receiver/worker split with SQS and DLQ | FR-016, ADR-0005, RB-05 | p95 acknowledgement < 2 s; DLQ alarm; duplicates absorbed | Not started |
| T-502 | Commit status `mergeguard/review` | FR-017 | States per [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) §5.6; usable as a required check | Not started |
| T-503 | Dismiss stale MergeGuard `REQUEST_CHANGES` reviews | FR-018, RB-11 | Fixing a flagged PR leaves no active blocking MergeGuard review | Not started |
| T-504 | Scanner suppressions, including the `N-SUPPRESSED` notice | FR-019 | Inline marker and path allow-list tested; suppressions listed in the review | Not started |
| T-505 | Remote Terraform state and OIDC-based deploy workflow | [DEPLOYMENT.md](DEPLOYMENT.md) §8 | Deploy from CI with approval; no static AWS keys in GitHub | Not started |
| T-506 | Alarms and AWS Budgets alert | NFR-007, TM-08 | Alarms fire in a test; budget alert configured | Not started |

## M6 — Distribution (Phase 3)

| ID | Task | Requirements | Acceptance criteria | Status |
| --- | --- | --- | --- | --- |
| T-601 | GitHub App authentication (new ADR for JWT signing) | FR-020, ADR-0006 | Reviews appear as `[bot]`; works on two or more repositories | Not started |
| T-602 | Inline line-level comments | FR-021 | Comments anchored only to lines present in the diff | Not started |
| T-603 | Per-repository JSON config | FR-022 | Bounds enforced; invalid config falls back to defaults with a notice | Not started |
