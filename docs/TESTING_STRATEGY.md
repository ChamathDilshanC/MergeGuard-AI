# MergeGuard AI — Testing Strategy

| Field | Value |
| --- | --- |
| Status | Draft v0.1 — unit, contract and integration tests for M1–M2 exist; AI evaluation (M3) and E2E (M4) are not built yet |
| Last updated | 2026-10-21 |
| Related | [PRD](PRD.md) · [Architecture](ARCHITECTURE.md) §13 · [Security](SECURITY.md) §8 · [AI system](AI_SYSTEM.md) §9 |

## 1. Objectives

1. Prove the security-critical paths deterministically: signature verification, secret scanning, the no-AI-on-secrets gate, and log redaction.
2. Run the full handler offline in CI, with no AWS, GitHub, or Gemini credentials.
3. Keep the zero-runtime-dependency rule ([ADR-0002](ADR/0002-zero-runtime-dependencies.md)). Test tools are **dev-only** and never packaged into the Lambda.
4. Validate behaviour against real services in a sandbox before each release.

## 2. Test layers

| Layer | Scope | Network | Runs in | Tooling |
| --- | --- | --- | --- | --- |
| Unit | Pure modules: `webhook`, `scanner`, `policy`, `render`, `ai_reviewer` validation, `config` parsing | None | CI, every PR | `pytest` |
| Contract | GitHub and Gemini clients against recorded, sanitised responses | None (fake transport) | CI | `pytest` + fixtures |
| Integration | `lambda_handler` end to end with a fake transport and a stubbed SSM client | None | CI | `pytest`, `unittest.mock` (stdlib) |
| Infrastructure | Terraform formatting, validation, policy scanning | None | CI | `terraform fmt`, `terraform validate`, `checkov` |
| AI evaluation | Prompt and model quality | Gemini (real) | Manual, before model or prompt changes | `scripts/ai_eval.py` ([AI_SYSTEM.md](AI_SYSTEM.md) §9) |
| End-to-end | Deployed stack + sandbox GitHub repository | Real | Manual, before release | Checklist in §7 |

Target ratio: mostly unit tests, a focused contract and integration layer, and a small manual E2E suite.

## 3. Test doubles and fixtures

- **Injectable HTTP transport.** `mergeguard/http.py` defines the `Transport` signature `(method, url, headers, body, timeout)` and the real `urllib_transport`. Tests pass `FakeTransport` from `tests/support.py`, which matches on method and URL regex, replays canned responses or exceptions, and records every call. `urlopen` is only replaced inside `mergeguard.http`, never globally.
- **SSM stub.** `config.SecretStore` takes a client factory. Tests use `FakeSsm`, whose `get_parameters` returns fixed test values.
- **Fixtures** live in `tests/fixtures/`; responses are mostly built in tests:

  | Fixture | Content |
  | --- | --- |
  | `webhooks/pull_request_opened.json`, `ping.json`, `issues_opened.json` | Trimmed GitHub payloads. Variants (other actions, drafts, bad fields) are derived in tests with `support.pull_request_payload(...)` |
  | GitHub files, reviews and user responses | Built in tests with `support.json_response` and `support.file_entry`, so each test shows the data it depends on |
  | `gemini/*.json` | Recorded Gemini responses (M3) |

- Fixtures must be **sanitised**: no real tokens, private repository names, or personal data. Use `owner/repo` and `octocat`.
- Webhook signatures in tests are computed at test time with a test-only secret. Never disable verification to make a test pass.

## 4. Secret scanner corpus and the fake-secret policy

The scanner is the product's main security control, so it has its own corpus in `tests/unit/scanner/`:

| Corpus | Purpose | Gate |
| --- | --- | --- |
| Positive | At least 3 variants per rule (`aws-access-key-id`, `private-key-pem` for RSA/EC/OPENSSH/DSA/PKCS#8/encrypted, `github-token`, `google-api-key`, `generic-assignment`) | 100% detected (SM-3) |
| Negative | Look-alikes: placeholder values, short strings, `os.environ[...]` lookups, removed lines (`-`), context lines, `+++` headers, documentation of patterns | ≤ 5% flagged (SM-3) |
| Regression | Every reported false positive or negative becomes a case | No regressions |
| Self-scan | Every text file in this repository is scanned (`test_self_scan.py`) | No hits except AWS's documented example key ID |

The suite enforces **0%** flagged negatives, stricter than the SM-3 gate.

**Fake-secret policy:**

- AWS: use only AWS's documented example key ID `AKIAIOSFODNN7EXAMPLE`.
- Token-shaped values for GitHub tokens, Google API keys, and PEM bodies are **built at test time** by concatenation (for example `"gh" + "p_" + "x" * 36`). This keeps realistic-looking credentials out of the repository and stops them tripping GitHub push protection or MergeGuard itself.
- Never paste a real credential into a test, even an expired one.

## 5. Infrastructure tests

| Check | Command | Gate |
| --- | --- | --- |
| Formatting | `terraform -chdir=terraform fmt -check -recursive` | Must pass |
| Validation | `terraform -chdir=terraform init -backend=false` then `terraform -chdir=terraform validate` | Must pass |
| Policy scan | `checkov -d terraform` | No unresolved high-severity findings; accepted findings documented inline with a reason |
| Plan review | `terraform -chdir=terraform plan` against the sandbox account | Manual review before every apply |

## 6. Security tests

| ID | Test | Expected |
| --- | --- | --- |
| ST-01 | Valid signature | Processed |
| ST-02 | Tampered body, same signature | `401`, zero outbound calls |
| ST-03 | Missing header / `sha1=` prefix / empty value | `401` |
| ST-04 | Base64-encoded body (`isBase64Encoded: true`) with valid signature | Processed |
| ST-05 | Secret findings present | Zero calls to the Gemini URL (asserted on the fake transport) |
| ST-06 | Log redaction: run every integration scenario while capturing logs | No test secret, test token, signature, diff line, or AI raw text appears in captured output |
| ST-07 | Model output containing `@someone`, a fake `mergeguard:` marker, and an 80,000-character body | Mentions neutralised, marker stripped, body ≤ 60,000 characters |
| ST-08 | Prompt-injection diff (unit level) | The diff is placed only inside the random boundary block; the system instruction is unchanged |
| ST-09 | Policy exhaustive table | `APPROVE` is never returned for any input combination |

## 7. End-to-end scenarios (manual, sandbox)

Run against a deployed stack and a **sandbox** repository that has the webhook configured. These turn the brief's demo scenario into a repeatable checklist.

| ID | Steps | Expected |
| --- | --- | --- |
| E2E-1 | Create the webhook | *Recent Deliveries* shows `ping` → `200`, body status `pong` |
| E2E-2 | New branch adds a line containing `AKIAIOSFODNN7EXAMPLE`; open a PR | Within SM-1: `T-SEC` review as `REQUEST_CHANGES`, or `COMMENT` + `T-FALLBACK` if the PR author owns the token. Logs: no `ai.completed`, no key value |
| E2E-3 | Push a commit that removes that line | New `T-AI` review for the new SHA; the removed line is not flagged (EC-09) |
| E2E-4 | PR with a deliberate bug (for example SQL string concatenation) | `T-AI` review; event per FR-010 |
| E2E-5 | Docs-only PR | `T-AI` with `LOW` → `COMMENT` |
| E2E-6 | Redeliver the E2E-5 delivery from GitHub | Response status `duplicate`; no second review |
| E2E-7 | `curl -i -X POST "$WEBHOOK_URL" -H "Content-Type: application/json" -H "X-GitHub-Event: pull_request" -H "X-Hub-Signature-256: sha256=0000" -d '{}'` | `401`, status `rejected` |
| E2E-8 | `curl -i "$WEBHOOK_URL"` (GET) | `404` from API Gateway |
| E2E-9 | Review CloudWatch logs for all runs | Only allow-listed fields ([DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) §7) |

Record the outcome (pass/fail, delivery IDs, date) in the release PR description.

## 8. Requirement coverage

| Requirement | Unit | Contract | Integration | E2E |
| --- | --- | --- | --- | --- |
| FR-001 | — | — | — | E2E-1, E2E-8 |
| FR-002 | ST-01 to ST-04 | — | ✓ | E2E-7 |
| FR-003 | Event and action filter | — | ✓ | E2E-1 |
| FR-004 | Diff bundling | Pagination, no-patch | ✓ | E2E-4 |
| FR-005 | Scanner corpus | — | — | E2E-2, E2E-3 |
| FR-006 | — | — | ST-05 | E2E-2 |
| FR-007 | Truncation | Gemini fixtures | ✓ | E2E-4 |
| FR-008 | Output validation | Invalid fixtures | ✓ | — |
| FR-009 | Snapshot tests, ST-07 | — | — | E2E-2 to E2E-5 |
| FR-010 | ST-09 policy table | `422` fallback | ✓ | E2E-2 |
| FR-011 | Config loader | — | ✓ | All |
| FR-012 | — | — | — | §5 checks |
| FR-013 | — | — | ST-06 | E2E-9 |
| FR-014 | Marker matching | Existing reviews | ✓ | E2E-6 |
| FR-015 | — | — | Dry-run test | — |

## 9. Quality gates (CI)

A PR can merge only when all of these hold:

1. `ruff check` and `ruff format --check` pass.
2. `pytest` passes on **Python 3.12**, the Lambda runtime version, even if developers use a newer local Python.
3. Line coverage of `src/` ≥ 85% (NFR-009).
4. Terraform `fmt` and `validate` pass, and `checkov` has no unresolved high-severity findings.
5. The Lambda package contains first-party code only (NFR-008).
6. The docs are updated when behaviour, configuration, or contracts change ([AGENTS.md](../AGENTS.md) §9).

## 10. Commands

| Purpose | Command | Status |
| --- | --- | --- |
| Install dev tools | `python -m pip install -r requirements-dev.txt` | Verified (M1) |
| Run tests | `python -m pytest` | Verified (M1) |
| Coverage | `python -m pytest --cov=src --cov-report=term-missing` | Verified (M1); fails below 85% |
| Lint / format check | `ruff check .` and `ruff format --check .` | Verified (M1) |
| Terraform checks | See §5 | `fmt` and `validate` Verified (M1); `checkov` Verified in CI (not run locally); plan review starts in T-401 |

Quality gates 1–5 in §9 are automated in `.github/workflows/ci.yml`. Gate 5 is the import check in `tests/unit/test_runtime_dependencies.py`. Gate 6 (docs updated) is a review item.
