# MergeGuard AI — Design System

| Field | Value |
| --- | --- |
| Status | Draft v0.1 — templates (§5), sanitisation, log conventions (§7) and rule IDs (§8) implemented in M2–M3 and snapshot-tested; the commit status (§5.6), `N-SUPPRESSED`, the queue and worker log events and the Phase 2 resource names in M5; inline comments (§5.6.2), `N-CONFIG`, `N-CONFIG-INVALID` and their log events in M6. All tested with fakes. Nothing is deployed |
| Last updated | 2026-10-21 |
| Related | [PRD](PRD.md) · [API spec](API_SPEC.md) · [AI system](AI_SYSTEM.md) |

## 1. Scope

MergeGuard AI is a **headless backend**. It has no web, mobile, or desktop interface, and none is planned (PRD §6.2). Its user-facing surface is the **Markdown review comment that GitHub renders on a pull request**. Operators also interact through HTTP responses, logs, Terraform outputs, and the documentation.

This design system therefore defines content, structure, and interface conventions. Conventional visual sections are marked *not applicable*:

| Conventional section | Applicability | What replaces it here |
| --- | --- | --- |
| Colour tokens, light/dark theme, contrast | Not applicable; GitHub controls theme and colours | Verdict tokens with mandatory text labels (§4.1) |
| Typography scale | Not applicable | Markdown heading hierarchy (§4.2) |
| Spacing, radii, elevation, grid | Not applicable | — |
| Responsive breakpoints | Not applicable; GitHub renders on web and mobile | Narrow-table rule: ≤ 4 columns (§4.3) |
| Navigation patterns | Not applicable | — |
| Motion / reduced motion | Not applicable | — |
| Forms and validation | Not applicable | Webhook input validation in [API_SPEC.md](API_SPEC.md) §2 |
| Screen inventory | Replaced | Surface inventory (§2) |
| Component inventory and states | Replaced | Comment templates (§5) and their states |

## 2. Surface inventory

| ID | Surface | Audience | Phase | Defined in |
| --- | --- | --- | --- | --- |
| S1 | PR review (summary comment) | PR author, reviewer | 1 | §5 |
| S2 | Commit status `mergeguard/review` | PR author, branch protection | 2 (implemented M5, not deployed) | §5.6 |
| S3 | Inline review comments | PR author | 3 (implemented M6, not deployed) | §5.6.2 |
| S4 | Webhook HTTP responses (visible in GitHub *Recent Deliveries*) | Repository admin | 1 | §6 |
| S5 | Structured logs | Operator | 1 | §7 |
| S6 | Terraform outputs and resource names | Operator | 1 | §8 |

## 3. Design principles

1. **Signal over noise.** One review per head SHA. Never post when there is nothing to say (`no_patches`). No celebration comments.
2. **Lead with the verdict.** The first line after the title states what happened and what the author must do.
3. **Actionable, specific, kind.** Each finding names a file, a line where available, and a fix. Never blame the author.
4. **Honest about limits.** Say when a review is partial, when AI was skipped or unavailable, and that AI can be wrong.
5. **Never reveal secrets.** Findings show rule, file, and line. They never show the value, not even partially beyond the masked hint.
6. **Accessible by default.** Emoji are decoration only. Every status has a text label. Headings are ordered and tables are narrow.

## 4. Content tokens

### 4.1 Verdict tokens

Render a token exactly as shown. The text label is mandatory and the emoji is optional decoration.

| Token | Rendered text | Used when |
| --- | --- | --- |
| `verdict.blocked` | 🚨 Changes requested — possible credentials found | Secret findings (FR-006) |
| `verdict.high` | 🔴 High risk | AI `risk_level = HIGH` |
| `verdict.medium` | 🟡 Medium risk | AI `risk_level = MEDIUM` |
| `verdict.low` | 🟢 Low risk | AI `risk_level = LOW` |
| `verdict.unavailable` | ⚪ AI review unavailable | FR-008 fallback |

| Token | Rendered text |
| --- | --- |
| `scan.passed` | Secret scan: passed |
| `scan.failed` | Secret scan: failed |
| `bugs.count` | Potential bugs: `<n>` |

### 4.2 Heading hierarchy

| Level | Use |
| --- | --- |
| `##` | Comment title (exactly one per comment) |
| `###` | Sections: *Findings*, *Detailed feedback*, *What to do now* |
| `####` | Not used; model output headings are demoted to bold text |
| `#` | Never; it is too dominant inside a GitHub timeline |

### 4.3 Layout and length rules

| Rule | Value |
| --- | --- |
| Maximum review body | 60,000 characters (GitHub limit 65,536) |
| Maximum summary | 500 characters |
| Maximum `feedback_markdown` after sanitisation | 20,000 characters |
| Maximum findings listed individually | 25; the remainder is summarised as "and *n* more" |
| Tables | ≤ 4 columns, header row required |
| Code | Fenced with a language tag; file paths in inline code |
| Footer | `<sub>`-wrapped single line |
| Hidden marker | First line: `<!-- mergeguard:v1 kind=<kind> sha=<short-sha> -->` |

## 5. Review comment templates

Templates live in `src/mergeguard/render.py` (Proposed) and are snapshot-tested. Placeholders appear as `<name>`.

### 5.1 `T-SEC` — security alert (FR-006)

Review event: `REQUEST_CHANGES`.

````markdown
<!-- mergeguard:v1 kind=security sha=<short-sha> -->
## 🚨 MergeGuard Security Alert

**Verdict:** Changes requested — possible credentials found in this pull request.

### Findings

| # | Finding | File | Line |
| --- | --- | --- | --- |
| 1 | AWS access key ID | `config/settings.py` | 42 |

### What to do now

1. **Revoke or rotate** each credential at its provider. It is already visible in this branch's history, so deleting the line is not enough.
2. Remove the value from the code and load it from AWS SSM Parameter Store or an environment variable.
3. Push a new commit. MergeGuard reviews it automatically.

<sub>AI review was skipped: this diff was not sent to any external AI service. · MergeGuard AI · commit `<short-sha>`</sub>
````

### 5.2 `T-AI` — AI code review (FR-007, FR-010)

Review event: `COMMENT` or `REQUEST_CHANGES`, per the policy.

````markdown
<!-- mergeguard:v1 kind=ai-review sha=<short-sha> -->
## 🤖 MergeGuard AI Code Review

**Verdict:** 🟡 Medium risk · **Potential bugs:** 2 · **Secret scan:** passed

**Summary:** <summary from model, sanitised>

### Detailed feedback

<feedback_markdown from model, sanitised>

<sub>Automated first-pass review by MergeGuard AI (model `<model-id>`). It can be wrong; a human reviewer decides whether to merge. · commit `<short-sha>`</sub>
````

### 5.3 `T-AIU` — AI review unavailable (FR-008)

Review event: `COMMENT`.

````markdown
<!-- mergeguard:v1 kind=ai-unavailable sha=<short-sha> -->
## 🤖 MergeGuard AI Code Review

**Verdict:** ⚪ AI review unavailable · **Secret scan:** passed

No credentials were detected in the added lines. The AI review could not be completed this time (<reason-category>). Push a new commit or ask the maintainer to redeliver the webhook to try again.

<sub>MergeGuard AI · commit `<short-sha>`</sub>
````

`<reason-category>` is one of the reasons in [AI_SYSTEM.md](AI_SYSTEM.md) §8, for example `timeout`, `rate_limited`, `invalid_output` or `not_configured`. Anything else renders as `unknown`.

### 5.4 Notices (appended inside `T-AI` / `T-SEC`)

| ID | Trigger | Text |
| --- | --- | --- |
| `N-PARTIAL-DIFF` | Diff exceeded `MERGEGUARD_MAX_DIFF_CHARS` | `> [!NOTE]` + "Partial AI review: the diff exceeded <n> characters. Not reviewed by AI: `<files>`." |
| `N-PARTIAL-FILES` | File cap reached | `> [!NOTE]` + "Only the first <n> changed files were checked." |
| `N-NO-PATCH` | Some files had no patch | `> [!NOTE]` + "Not reviewed (binary or too large): `<files>`." |
| `N-SUPPRESSED` | One or more findings were suppressed (FR-019) | `> [!NOTE]` + "<n> finding(s) suppressed by `mergeguard:ignore-secret` or path allow-list. These lines were not sent to the AI.", then one `> - <rule label>: `<file>` line <n> (inline marker \| path allow-list)` bullet per suppression, at most 25, then "…and <n> more." |
| `N-CONFIG` | A valid repository configuration changed a setting (FR-022) | `> [!NOTE]` + "Repository configuration `.github/mergeguard.json` applied: `block_on=HIGH`, `ai_review=off`, `ignore_paths=2 pattern(s)`, `inline_comments=secrets`." (only the settings that were set) |
| `N-CONFIG-INVALID` | The file exists but is invalid or outside the bounds | `> [!NOTE]` + "The repository configuration `.github/mergeguard.json` was ignored (<reason>), so the default settings apply." `<reason>` is `invalid_json`, `too_large`, `unknown_key`, `unsupported_version`, `invalid_field` or `out_of_bounds`; anything else renders as `unknown`. Never file content |

### 5.5 `T-FALLBACK` — own-PR note (FR-010)

When GitHub rejects `REQUEST_CHANGES` with `422` because the token owner authored the PR, the same template is posted as `COMMENT` with this line inserted directly under the verdict:

```markdown
> [!IMPORTANT]
> Changes are requested. GitHub does not allow this bot identity to formally request changes on this pull request, so this review is posted as a comment.
```

### 5.6 Commit status (Phase 2, FR-017)

Context `mergeguard/review`, set on the head SHA. Off unless `MERGEGUARD_COMMIT_STATUS` is `true` (Terraform sets it by default), because the token then needs *Commit statuses: Read and write*.

| State | `description` (≤ 140 chars) | When |
| --- | --- | --- |
| `pending` | MergeGuard review in progress | The PR is known to need a review: after the file list and the duplicate check, before the scan and the AI call |
| `failure` | Possible credentials found — see review | Secret findings, even if GitHub made the review a comment (own-PR fallback) |
| `failure` | Changes requested by AI review — see review | The policy decided `REQUEST_CHANGES` from the AI result |
| `success` | No blocking issues found | The policy decided `COMMENT` (low risk, or AI unavailable), or the PR has no reviewable patches |
| `error` | MergeGuard could not complete the review | The run failed (best effort; SQS then retries) |

- The state follows the **decision**, not the posted event.
- A redelivery or retry that finds its own review sets the final state from that review and never `pending`, so a crash between the review and the status cannot leave a check stuck.
- A missing permission is logged (`status.failed`) and never stops the review.
- With a dry run nothing is written.

### 5.6.1 Dismissal message (FR-018)

When a later review of the same pull request has no blocking result, each earlier `REQUEST_CHANGES` review of MergeGuard is dismissed. If that later review is *AI review unavailable* (`T-AIU`), only earlier `T-SEC` reviews are dismissed and an earlier `T-AI` block stays. The message is: "MergeGuard AI: superseded by the review of commit `<short-sha>`, which found no blocking issues. See the latest MergeGuard review."

### 5.6.2 Inline comments (Phase 3, FR-021)

Posted with the review, anchored to a line of the diff on the right side. One comment per line.

- **Secret finding** (`T-SEC`):

  ```markdown
  🚨 **Possible credential: AWS access key ID, GitHub token.** Revoke or rotate it at its provider first: it is already visible in this branch's history. Then load it from AWS SSM Parameter Store or an environment variable.
  ```

  It names each rule that matched the line and never the value. At most 25 per review.
- **AI finding** (`T-AI`, mode `all` only): `🤖 <message>` where the message is sanitised like all model text (§5.7). Several findings on one line are joined by a blank line. At most 10 per review.
- A comment is never anchored to a line outside the diff, nor to a line that was withheld from the AI.
- The summary review remains the source of truth and lists every finding; if GitHub refuses the comments, the review is posted without them.

### 5.7 Sanitisation of model output

- Replace `@` before a GitHub handle pattern with `@​` (zero-width space) so nobody is notified.
- Convert any Markdown heading in model output (`#` to `######`) into bold text, so it cannot compete with the template's own headings.
- Strip HTML comments that look like the `mergeguard:` marker, closed or not, so model output cannot spoof one. Escape any other `<!--` so the model cannot hide text from readers.
- Truncate to the §4.3 limits and append "(truncated)".

## 6. HTTP response conventions

- JSON envelope `{"status", "reason", "delivery_id"}` (see [API_SPEC.md](API_SPEC.md) §2.3 for every case).
- `status` values are lower-case nouns or adjectives: `rejected`, `pong`, `ignored`, `skipped`, `duplicate`, `reviewed`, `dry_run`, `accepted`, `error`.
- `reason` values are `snake_case` machine-readable codes or a review event in upper case.
- No human prose, stack traces, or internal identifiers beyond `delivery_id`.

## 7. Log event conventions

One JSON object per line. Field names are `snake_case`.

| Field | Type | Always present | Example |
| --- | --- | --- | --- |
| `ts` | string (ISO 8601 UTC) | Yes | `2026-10-21T08:00:00.123Z` |
| `level` | string | Yes | `INFO` |
| `event` | string | Yes | `review.posted` |
| `delivery_id` | string | When known | GUID |
| `repo` | string | When known | `owner/repo` |
| `pr` | integer | When known | `7` |
| `head_sha` | string (short) | When known | `a1b2c3d` |
| `action` | string | When known | `synchronize` |
| `decision` | string | On decision | `REQUEST_CHANGES` |
| `rule_ids` | array of strings | On findings | `["aws-access-key-id"]` |
| `risk_level` | string | On AI result | `LOW` |
| `model` | string | On AI call | `gemini-2.5-flash` |
| `prompt_version` | string | On AI call | `review-v1` |
| `reason` | string | On skip or failure | `rate_limited` |
| `duration_ms` | integer | On completion | `8421` |
| `github_event` | string | On receipt | `pull_request` |
| `status` | string | On completion | `reviewed` |
| `http_status` | integer | On completion and GitHub errors | `422` |
| `file_count` | integer | After fetching files | `12` |
| `body_length` | integer | Dry run | `1834` |
| `error_type` | string | Unhandled errors | `RuntimeError` |
| `commit_state` | string | `status.set` | `failure` |
| `suppressed` | integer | `scan.completed` | `1` |
| `dismissed` | integer | `review.dismissed` | `2` |
| `inline_comments` | integer | `review.posted` | `3` |
| `config_keys` | integer | `repo_config.applied` | `2` |

**Event names:**

- **Webhook:** `webhook.received`, `webhook.rejected`, `webhook.ignored`, `webhook.completed`, `config.failed`.
- **Pipeline:** `files.fetched`, `scan.completed` (with `rule_ids` and `suppressed`), `review.posted` (with `inline_comments`), `review.fallback_comment`, `review.skipped` (reasons include `no_patches`, `stale_head`, `pr_closed`), `review.duplicate`, `review.dry_run`.
- **Signals:** `status.set` (with `commit_state`), `status.failed` (WARNING, with `reason` and `http_status`), `review.dismissed` (with `dismissed`), `review.dismissal_failed` (WARNING), `review.inline_dropped` (GitHub refused the inline comments; the review was posted without them), `repo_config.applied` (with `config_keys`), `repo_config.invalid` (WARNING, with `reason` as a category, never content).
- **Queue and worker:** `queue.enqueued` (receiver), `queue.failed` (receiver, ERROR), `worker.received`, `worker.completed` (with `status`, `reason`, `duration_ms`), `worker.rejected` (ERROR, `reason` `malformed_message`).
- **GitHub errors:** `github.auth_failed`, `github.forbidden`, `github.invalid_request`, `github.rate_limited`, `github.unavailable`, `github.unexpected`.
- **AI:** `ai.completed` (INFO, with `risk_level`, `model`, `prompt_version`, `duration_ms`) and `ai.failed` (WARNING, with `reason` instead of `risk_level`).
- **Other:** `error.unhandled`.

`delivery_id` and `github_event` come from request headers, so they are logged only when they match the expected format; otherwise the value `invalid` is used.

**Never logged:** request body, headers other than event and delivery ID, signature, tokens, keys, diff or patch text, AI prompt or raw response, matched secret values.

## 8. Naming conventions

| Thing | Convention | Examples |
| --- | --- | --- |
| AWS resources | `mergeguard-<purpose>` (kebab case); names from the brief kept | `mergeguard-ai-pr-reviewer` (the receiver), `mergeguard-ai-pr-worker`, `mergeguard-webhook-gateway`, `mergeguard-review-requests`, `mergeguard-review-requests-dlq`, `mergeguard-alerts` |
| IAM role and policy | Names from the brief; the worker follows the same pattern | `mergeguard_lambda_exec_role`, `mergeguard_lambda_policy` (receiver), `mergeguard_worker_exec_role`, `mergeguard_worker_policy`; the CI role is `mergeguard-github-deploy` |
| CloudWatch alarms | `mergeguard-<what>` | `mergeguard-review-dlq-not-empty`, `mergeguard-webhook-5xx`, `mergeguard-receiver-lambda-errors`, `mergeguard-worker-lambda-errors` |
| SSM parameters | `<prefix>/<snake_case>` | `/mergeguard/github_token` |
| Environment variables | `MERGEGUARD_<UPPER_SNAKE>` | `MERGEGUARD_GEMINI_MODEL` |
| Terraform resources and variables | `snake_case` | `aws_lambda_function.pr_reviewer`, `log_retention_days` |
| Python modules and functions | `snake_case`; classes `PascalCase` | `scanner.scan_added_lines`, `SecretFinding` |
| Scanner rule IDs | kebab case, stable once released | `aws-access-key-id`, `private-key-pem`, `github-token`, `google-api-key`, `generic-assignment` |
| Log events | `<area>.<past_tense_or_state>` | `review.posted` |

## 9. Accessibility and readability checklist

- [ ] Every verdict, risk level, and status has a text label; no meaning depends on emoji or colour.
- [ ] Exactly one `##` heading; sections use `###`; no skipped levels.
- [ ] Tables have a header row and ≤ 4 columns, so they stay readable on GitHub mobile.
- [ ] Link text describes its destination; no bare "click here".
- [ ] File paths and identifiers are in inline code; code samples have a language tag.
- [ ] Sentences are short and in plain English. Avoid jargon such as "SAST" without context.
- [ ] The footer states that AI output can be wrong and that a human decides.

## 10. User flows

**PR author:**

1. Push or open a PR.
2. Within about a minute, see a MergeGuard review (`T-SEC`, `T-AI` or `T-AIU`).
3. Fix the issues (rotating credentials first if `T-SEC`) and push.
4. See a new review for the new head SHA. From Phase 2, the commit status updates and stale blocking reviews are dismissed.
5. If a finding is a false positive (a fixture, a documented example), add `mergeguard:ignore-secret` to that line, or ask the operator to add the path to the allow-list. The review will list the suppression.

**Operator:** deploy ([DEPLOYMENT.md](DEPLOYMENT.md)) → add the webhook → confirm `ping` returns `200 pong` in *Recent Deliveries* → open a test PR → check logs.

## 11. Localisation

English is the only language ([OQ-03](PRD.md#17-open-questions), decided 2026-10-22; PRD A-05). The brief's security alert was written in Sinhala; that text is not carried over. Templates are kept as named strings, so a second language would be a new set of templates and a new decision, and the repository configuration's `language` key accepts only `en` until then. Model output language follows the prompt.

## 12. Consistency checks

- [ ] Template IDs in code match this document (`T-SEC`, `T-AI`, `T-AIU`, `T-FALLBACK`, `N-*`).
- [ ] Snapshot tests exist for each template and notice.
- [ ] Rule IDs in the scanner match §8 and the PRD.
- [ ] Log events in code are a subset of §7; new events are added here first.
