# MergeGuard AI — Design System

| Field | Value |
| --- | --- |
| Status | Draft v0.1 — templates (§5, except §5.6 and `N-SUPPRESSED`), sanitisation, log conventions (§7) and rule IDs (§8) implemented in M2–M3 and snapshot-tested |
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
| S2 | Commit status `mergeguard/review` | PR author, branch protection | 2 | §5.6 |
| S3 | Inline review comments | PR author | 3 | FR-021 (template TBD in Phase 3) |
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
| `N-SUPPRESSED` | Phase 2 suppressions | `> [!NOTE]` + "<n> finding(s) suppressed by `mergeguard:ignore-secret` or path allow-list." |

### 5.5 `T-FALLBACK` — own-PR note (FR-010)

When GitHub rejects `REQUEST_CHANGES` with `422` because the token owner authored the PR, the same template is posted as `COMMENT` with this line inserted directly under the verdict:

```markdown
> [!IMPORTANT]
> Changes are requested. GitHub does not allow this bot identity to formally request changes on this pull request, so this review is posted as a comment.
```

### 5.6 Commit status (Phase 2, FR-017)

| State | `description` (≤ 140 chars) |
| --- | --- |
| `pending` | MergeGuard review in progress |
| `failure` | Possible credentials found — see review |
| `failure` | Changes requested by AI review — see review |
| `success` | No blocking issues found |
| `error` | MergeGuard could not complete the review |

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

**Event names:**

- **Webhook:** `webhook.received`, `webhook.rejected`, `webhook.ignored`, `webhook.completed`, `config.failed`.
- **Pipeline:** `files.fetched`, `scan.completed`, `review.posted`, `review.fallback_comment`, `review.skipped`, `review.duplicate`, `review.dry_run`.
- **GitHub errors:** `github.auth_failed`, `github.forbidden`, `github.invalid_request`, `github.rate_limited`, `github.unavailable`, `github.unexpected`.
- **AI:** `ai.completed` (INFO, with `risk_level`, `model`, `prompt_version`, `duration_ms`) and `ai.failed` (WARNING, with `reason` instead of `risk_level`).
- **Other:** `error.unhandled`.

`delivery_id` and `github_event` come from request headers, so they are logged only when they match the expected format; otherwise the value `invalid` is used.

**Never logged:** request body, headers other than event and delivery ID, signature, tokens, keys, diff or patch text, AI prompt or raw response, matched secret values.

## 8. Naming conventions

| Thing | Convention | Examples |
| --- | --- | --- |
| AWS resources | `mergeguard-<purpose>` (kebab case); names from the brief kept | `mergeguard-ai-pr-reviewer`, `mergeguard-webhook-gateway` |
| IAM role and policy | Names from the brief | `mergeguard_lambda_exec_role`, `mergeguard_lambda_policy` |
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

**Operator:** deploy ([DEPLOYMENT.md](DEPLOYMENT.md)) → add the webhook → confirm `ping` returns `200 pong` in *Recent Deliveries* → open a test PR → check logs.

## 11. Localisation

English is the default and only language in Phase 1 (PRD A-05). The brief's security alert was written in Sinhala, so whether to offer a Sinhala variant is an open question ([OQ-03](PRD.md#17-open-questions)). Templates are kept as named strings to make a later `MERGEGUARD_COMMENT_LANGUAGE` setting (Proposed, Phase 3 via FR-022) straightforward. Model output language would follow the prompt.

## 12. Consistency checks

- [ ] Template IDs in code match this document (`T-SEC`, `T-AI`, `T-AIU`, `T-FALLBACK`, `N-*`).
- [ ] Snapshot tests exist for each template and notice.
- [ ] Rule IDs in the scanner match §8 and the PRD.
- [ ] Log events in code are a subset of §7; new events are added here first.
