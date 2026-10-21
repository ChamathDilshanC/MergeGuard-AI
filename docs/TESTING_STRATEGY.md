# MergeGuard AI — Testing Strategy

| Field | Value |
| --- | --- |
| Status | Draft v0.1 — unit, contract and integration tests for M1–M3 exist (Gemini mocked); Phase 2 (M5) adds queue, worker, commit status, dismissal and suppression tests, plus workflow and bootstrap checks; Phase 3 (M6) adds RS256 signing, GitHub App authentication, inline comment and repository configuration tests with fakes; AI evaluation (T-304, blocked on OQ-05) and E2E (needs a deployment, T-402) are not built yet |
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
| Integration | The receiver and the SQS worker end to end with a fake transport, stubbed SSM and a fake SQS client | None | CI | `pytest`, `unittest.mock` (stdlib) |
| Infrastructure | Terraform formatting, validation, mocked-provider plan tests of the stack and the bootstrap stack, policy scanning | None | CI | `terraform fmt`, `terraform validate`, `terraform test`, `checkov` |
| Workflow | The GitHub Actions workflows: pinned actions, OIDC instead of keys, approval before apply | None | CI | `pytest` (text checks), `actionlint` run by hand |
| AI evaluation | Prompt and model quality | Gemini (real) | Manual, before model or prompt changes | `scripts/ai_eval.py` ([AI_SYSTEM.md](AI_SYSTEM.md) §9) |
| End-to-end | Deployed stack + sandbox GitHub repository | Real | Manual, before release | Checklist in §7 |

Target ratio: mostly unit tests, a focused contract and integration layer, and a small manual E2E suite.

## 3. Test doubles and fixtures

- **Injectable HTTP transport.** `mergeguard/http.py` defines the `Transport` signature `(method, url, headers, body, timeout)` and the real `urllib_transport`. Tests pass `FakeTransport` from `tests/support.py`, which matches on method and URL regex, replays canned responses or exceptions, and records every call. `urlopen` is only replaced inside `mergeguard.http`, never globally.
- **SSM stub.** `config.SecretStore` takes a client factory. Tests use `FakeSsm`, whose `get_parameters` returns fixed test values.
- **SQS fake.** `review_queue.ReviewQueue` takes a client factory. Tests use `FakeSqs`, which records `send_message` calls or raises.
- **Harnesses.** `Harness` (the receiver or the inline handler) and `WorkerHarness` (the SQS worker) in `tests/support.py` run the real handlers against the fakes and capture the JSON logs. `github_api(...)` fakes files, reviews, the pull request state, commit statuses and dismissals, and mocks Gemini.
- **Fixtures** live in `tests/fixtures/`; responses are mostly built in tests:

  | Fixture | Content |
  | --- | --- |
  | `webhooks/pull_request_opened.json`, `ping.json`, `issues_opened.json` | Trimmed GitHub payloads. Variants (other actions, drafts, bad fields) are derived in tests with `support.pull_request_payload(...)` |
  | GitHub files, reviews and user responses | Built in tests with `support.json_response` and `support.file_entry`, so each test shows the data it depends on |
  | `gemini/ok_low.json`, `ok_high.json`, `invalid_json.json`, `schema_mismatch.json`, `blocked.json` | Gemini responses in the real response shape (M3). Other variants are built with `support.gemini_response(...)` |

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
| Plan tests | `terraform -chdir=terraform test` (AWS provider mocked; no credentials) | Must pass; asserts the SECURITY.md §5.3 receiver and worker policies, the queue and redrive settings, throttling, route, retention, alarms, the budget and SSM placeholders |
| Bootstrap checks | `terraform -chdir=terraform/bootstrap init -backend=false`, then `validate` and `test` | Must pass; asserts the state bucket protections, the OIDC trust policy (exact subjects, no wildcard) and the scope of the deploy role |
| Policy scan | `checkov -d terraform` | No unresolved high-severity findings; accepted findings documented inline with a reason. CI also fails on a checkov **parsing error**, because checkov skips an unparsable file and still exits 0 |
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
| ST-10 | A finding suppressed by the inline marker or the path allow-list | The PR is not blocked; the review lists the rule, file, line and method (`N-SUPPRESSED`); the flagged value is absent from the review, the logs and the Gemini request body, which holds a withheld-line stub instead. For a suppressed private-key header, the key lines after it are withheld too |
| ST-11 | A suppressed finding next to an unsuppressed one | `REQUEST_CHANGES`, only the unsuppressed finding in the table, the suppressed one in the notice, zero Gemini calls |
| ST-12 | Dismissal safety: older blocking reviews by another user, without the marker, already dismissed, or on the current head | Only the bot's own older `CHANGES_REQUESTED` reviews carrying the marker are dismissed; after an AI failure only the secret blocks are |
| ST-13 | Queue messages: malformed, wrong schema, bad repository, SHA or action, and a message with extra author or title fields | The receiver's message holds metadata only; the worker rejects every malformed message (it is retried, then dead-lettered) and makes no GitHub or Gemini call |
| ST-14 | Receiver with only the webhook secret readable | Works; the receiver never asks SSM for the token or the Gemini key. The worker never asks for the webhook secret |
| ST-15 | The RS256 signer: PKCS#1 and PKCS#8 keys, tampered messages, a wrong private exponent, a non-invertible blinding factor, malformed DER, encrypted or undersized keys | Valid signatures verify; every malformed key is refused without echoing it; a faulty key never releases a signature. A one-off cross-check with OpenSSL 3.5 (2048 and 3072 bits, both formats) accepted every signature and rejected a tampered message |
| ST-16 | App tokens: one repository and minimal permissions per request; cached and renewed; separate per repository, installation and permission set; error mapping; a rotated key | Matches ADR-0010; no token, JWT or key in the logs; the personal access token is never read in App mode; `GET /user` is never called |
| ST-17 | Repository configuration written into the pull request's **head** (hostile) while the base has none, or other content | Only the base commit is requested; the head's file has no effect; an invalid, unknown-key, out-of-bounds or oversized file is ignored with a notice and never fails the review; the file cannot disable the secret scan or add suppressions |
| ST-18 | Inline comment anchoring: lines outside the diff, files not in the diff, files truncated out of the AI input, lines withheld under ADR-0008, malformed findings | No comment is anchored to any of them; secret comments never contain the value |
| ST-19 | GitHub refuses the inline comments (`422`) | The review is posted again without them and keeps its verdict; the own-PR fallback still works afterwards, with the comments retried first |

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

| E2E-10 | After the Phase 2 deploy, redeliver a `pull_request` webhook | The delivery shows `202 accepted` in *Recent Deliveries* within 2 s (SM-2, FR-016), and the review appears. Check the p95 over several deliveries |
| E2E-11 | Flag a PR with a secret, then push a fix; with the token's *Commit statuses* permission | `mergeguard/review` goes `pending` then `failure`; after the fix it goes `pending` then `success`, and the earlier blocking review is dismissed with a message naming the new commit |
| E2E-12 | Add `mergeguard:ignore-secret` to the fake key line from E2E-2 | `T-AI` or `T-AIU` review with the suppression listed; no `T-SEC` |
| E2E-13 | `aws cloudwatch set-alarm-state --alarm-name mergeguard-review-dlq-not-empty --state-value ALARM --state-reason test`, and the same for the other alarms | The notification reaches `alert_email`. Then send a malformed message to the queue and check that it reaches the dead-letter queue after three receives and the real alarm fires |
| E2E-14 | After setting `github_app_id` and installing the App on two repositories, open a PR on each | The reviews are authored by `<app-slug>[bot]`; each PR gets its own token request; a redelivery is a duplicate; the own-PR `422` no longer occurs |
| E2E-15 | With `inline_comments = secrets`, push the fake key from E2E-2 | The security review carries an inline comment on the offending line and never shows the value. If GitHub refuses it, the review still appears (`review.inline_dropped`) |
| E2E-16 | Commit `.github/mergeguard.json` with `block_on = HIGH` to the base branch, then open a PR with a MEDIUM-risk change; then change the file only in the PR branch | The base file is applied and listed in the review; the PR's own edit of the file has no effect until it is merged |

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
| FR-012 | `terraform test` plan assertions; `test_infra_contract.py` | — | — | §5 checks |
| T-505 | `test_workflows.py`; `terraform/bootstrap` plan tests | — | — | First CI deploy (T-402) |
| T-506 | Alarm and budget plan assertions | — | — | E2E-13 |
| FR-013 | — | — | ST-06 | E2E-9 |
| FR-014 | Marker matching | Existing reviews | ✓ | E2E-6 |
| FR-015 | — | — | Dry-run test | — |
| FR-016 | Queue message, settings | Message schema | Receiver `202`, worker, retries, stale head, ST-13, ST-14 | E2E-10 (latency, DLQ) |
| FR-017 | Status texts | `POST /statuses` body, no retry on 5xx | Every verdict, error status, missing permission, duplicate healing | E2E-11 |
| FR-018 | — | `PUT …/dismissals` body | ST-12, failed and retried dismissal, dry run | E2E-11 |
| FR-019 | Scanner suppressions, glob rules, withholding, notice snapshot, settings validation | — | ST-10, ST-11 | E2E-12 |
| FR-020 | RS256 signer, token minting and caching, settings | Token request and `GET /app` | App-mode review, bot-login duplicate and dismissal checks, per-repository tokens, installation ID, ST-15, ST-16 | E2E-14 |
| FR-021 | Inline module, line visibility, finding validation, prompts | Review `comments` body, `422` handling | Secret and AI comments, caps, modes, ST-18, ST-19 | E2E-15 |
| FR-022 | Config parser and bounds | Contents call | Base-commit read, notices, thresholds, AI off, ignored paths, ST-17 | E2E-16 |

## 9. Quality gates (CI)

A PR can merge only when all of these hold:

1. `ruff check` and `ruff format --check` pass.
2. `pytest` passes on **Python 3.12**, the Lambda runtime version, even if developers use a newer local Python.
3. Line coverage of `src/` ≥ 85% (NFR-009).
4. Terraform `fmt` and `validate` pass for the stack and the bootstrap stack, the plan tests pass, and `checkov` has no unresolved high-severity findings.
5. The Lambda package contains first-party code only (NFR-008).
6. The docs are updated when behaviour, configuration, or contracts change ([AGENTS.md](../AGENTS.md) §9).

## 10. Commands

| Purpose | Command | Status |
| --- | --- | --- |
| Install dev tools | `python -m pip install -r requirements-dev.txt` | Verified (M1) |
| Run tests | `python -m pytest` | Verified (M1) |
| Coverage | `python -m pytest --cov=src --cov-report=term-missing` | Verified (M1); fails below 85% |
| Lint / format check | `ruff check .` and `ruff format --check .` | Verified (M1) |
| Terraform checks | See §5 | `fmt`, `validate` and `terraform test` Verified (M4, M5, including the bootstrap stack); `checkov` Verified in CI (green on the M4 PR); real plan review starts with T-402 |
| Workflow lint | `actionlint .github/workflows/*.yml` | Run by hand in M5 (actionlint 1.7.12); not in CI |

Quality gates 1–5 in §9 are automated in `.github/workflows/ci.yml`. Gate 5 is the import check in `tests/unit/test_runtime_dependencies.py`. Gate 6 (docs updated) is a review item.
