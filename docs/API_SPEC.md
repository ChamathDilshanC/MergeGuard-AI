# MergeGuard AI — API Specification

| Field | Value |
| --- | --- |
| Status | Draft v0.1 — contracts for implementation; **no endpoint is deployed yet** |
| Last updated | 2026-10-21 |
| Related | [Architecture](ARCHITECTURE.md) · [AI system](AI_SYSTEM.md) · [Security](SECURITY.md) |

MergeGuard exposes **one inbound endpoint** (the GitHub webhook receiver) and calls **three outbound APIs**: the GitHub REST API, the Google Gemini API, and AWS SSM. It has no user-facing REST API, SDK, or CLI.

## 1. Conventions

- All traffic is HTTPS. Request and response bodies are UTF-8 JSON.
- Field and header names below are exact. Path parameters are written `{like_this}`.
- Inbound response bodies use one envelope:

  ```json
  { "status": "reviewed", "reason": "optional machine-readable detail", "delivery_id": "GUID from X-GitHub-Delivery" }
  ```

- Inbound responses never echo request content, secrets, stack traces, or internal error messages.

## 2. Inbound — `POST /webhook`

**Status:** Planned (brief). Served by API Gateway HTTP API → Lambda (`AWS_PROXY`, payload format `2.0`). The full URL is the Terraform output `github_webhook_url`.

### 2.1 Request headers (set by GitHub)

| Header | Required | Use |
| --- | --- | --- |
| `X-Hub-Signature-256` | Yes | `sha256=<hex HMAC-SHA256 of raw body>`; verified before anything else (FR-002) |
| `X-GitHub-Event` | Yes | Must be `pull_request` or `ping` (FR-003) |
| `X-GitHub-Delivery` | Yes | Delivery GUID; used as `delivery_id` in logs and responses |
| `Content-Type` | Yes | Must be `application/json`. The webhook must be configured with this content type; `application/x-www-form-urlencoded` is not supported |
| `User-Agent` | — | `GitHub-Hookshot/...`; informational only, not trusted |

API Gateway payload v2.0 lowercases header names. The handler must look headers up case-insensitively and base64-decode `body` when `isBase64Encoded` is `true`.

### 2.2 Request body — fields used

Only these fields of the GitHub `pull_request` event payload are read. Everything else is ignored.

| JSON path | Type | Use |
| --- | --- | --- |
| `action` | string | Allow-list: `opened`, `synchronize` (brief); `reopened`, `ready_for_review` (Proposed) |
| `pull_request.number` | integer | PR number for API calls |
| `pull_request.head.sha` | string | Review `commit_id`; idempotency key (FR-014) |
| `pull_request.draft` | boolean | Skip drafts (Proposed, OQ-09) |
| `pull_request.user.login` | string | Logging; own-PR detection |
| `repository.full_name` | string | `owner/repo` for API paths |
| `installation.id` | integer | Phase 3 only (GitHub App) |

### 2.3 Responses

| Situation | Status | `status` field | `reason` examples |
| --- | --- | --- | --- |
| Missing or invalid signature | `401` | `rejected` | `invalid_signature` |
| Valid signature, body is not valid JSON | `400` | `rejected` | `malformed_json` |
| `ping` event | `200` | `pong` | — |
| Event other than `pull_request` | `200` | `ignored` | `event_not_supported` |
| Action not in allow-list, or draft PR | `200` | `ignored` | `action_not_supported`, `draft` |
| No file with a patch | `200` | `skipped` | `no_patches` |
| Review already exists for this head SHA | `200` | `duplicate` | — |
| Review posted (Phase 1, synchronous) | `200` | `reviewed` | `REQUEST_CHANGES`, `COMMENT` |
| Dry run completed | `200` | `dry_run` | decision value |
| Work accepted for async processing (Phase 2) | `202` | `accepted` | — |
| Configuration, SSM, GitHub auth, rate-limit or unexpected error | `500` | `error` | `internal` (details only in logs) |

Ignored events deliberately return `2xx`, so GitHub's *Recent Deliveries* list only shows real failures. Any path or method other than `POST /webhook` gets API Gateway's own `404` and never reaches Lambda.

### 2.4 Example

```http
POST /webhook HTTP/1.1
Content-Type: application/json
X-GitHub-Event: pull_request
X-GitHub-Delivery: 72d3162e-cc78-11e3-81ab-4c9367dc0958
X-Hub-Signature-256: sha256=<hex digest>

{"action":"opened","pull_request":{"number":7,"draft":false,"head":{"sha":"<40-hex>"},"user":{"login":"octocat"}},"repository":{"full_name":"owner/repo"}}
```

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"status":"reviewed","reason":"COMMENT","delivery_id":"72d3162e-cc78-11e3-81ab-4c9367dc0958"}
```

## 3. Outbound — GitHub REST API

**Base URL:** `https://api.github.com`

**Common headers:**

| Header | Value |
| --- | --- |
| `Authorization` | `Bearer <github_token from SSM>` |
| `Accept` | `application/vnd.github+json` |
| `X-GitHub-Api-Version` | `2022-11-28` (pin; review when GitHub publishes a newer version) |
| `User-Agent` | `MergeGuard-AI-Bot` (from the brief) |
| `Content-Type` | `application/json` (requests with a body) |

### 3.1 Endpoints

| Purpose | Method and path | Phase | Notes |
| --- | --- | --- | --- |
| List changed files | `GET /repos/{owner}/{repo}/pulls/{pull_number}/files?per_page=100&page={n}` | 1 | Follow pages until fewer than 100 items or `MERGEGUARD_MAX_FILES` is reached. GitHub returns at most 3,000 files. `patch` is absent for binary or very large files |
| List reviews (duplicate check) | `GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews?per_page=100` | 1 | Match `commit_id` and the `mergeguard:v1` marker in `body` |
| Create review | `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` | 1 | Body below |
| Dismiss a review | `PUT /repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}/dismissals` | 2 | Body: `{"message": "...", "event": "DISMISS"}` (FR-018) |
| Create commit status | `POST /repos/{owner}/{repo}/statuses/{sha}` | 2 | `context: "mergeguard/review"` (FR-017) |

**Create-review request body:**

```json
{
  "commit_id": "<pull_request.head.sha>",
  "event": "COMMENT",
  "body": "<!-- mergeguard:v1 kind=ai-review sha=<short-sha> -->\n## 🤖 MergeGuard AI Code Review\n..."
}
```

`event` is `COMMENT` or `REQUEST_CHANGES`; MergeGuard never sends `APPROVE` (PRD §6.2). `body` must not exceed 60,000 characters (FR-009).

### 3.2 Error mapping

| GitHub response | Meaning | MergeGuard behaviour |
| --- | --- | --- |
| `401` | Token invalid or expired | Log `github.auth_failed`; respond `500` |
| `403` with rate-limit headers, or `429` | Rate limited | One retry if time allows, otherwise `500` |
| `403` otherwise / `404` | Missing permission, or repository not covered by the token | Log `github.forbidden`; respond `500` |
| `422` on create review with `REQUEST_CHANGES` | Typically "cannot request changes on your own pull request" | Retry once as `COMMENT` with fallback note (FR-010) |
| `422` otherwise | Invalid body (for example too long) | Log `github.invalid_request`; respond `500` |
| `5xx` | GitHub outage | One retry if time allows, otherwise `500` |

### 3.3 Token permissions (fine-grained PAT, Phase 1)

| Permission | Access | Why |
| --- | --- | --- |
| Repository access | Only selected repositories | Least privilege |
| Pull requests | Read and write | List files and reviews; create and dismiss reviews |
| Contents | Read | Listed in the brief; keep until tests confirm it is not needed |
| Metadata | Read (always included) | Required by GitHub |
| Commit statuses | Read and write | Phase 2 (FR-017) |

## 4. Outbound — Google Gemini API

**Status:** Planned (brief), with the key moved into a header (RB-07).

| Item | Value |
| --- | --- |
| Method and URL | `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` |
| `{model}` | `MERGEGUARD_GEMINI_MODEL`, default `gemini-2.5-flash` |
| Auth header | `x-goog-api-key: <gemini_api_key from SSM>` — never in the query string |
| Timeout | Derived from the remaining Lambda time, reserving ≥ 5 s for posting |

**Request body (shape):**

```json
{
  "systemInstruction": { "parts": [{ "text": "<system prompt, AI_SYSTEM.md section 4>" }] },
  "contents": [{ "role": "user", "parts": [{ "text": "<delimited, size-limited diff>" }] }],
  "generationConfig": {
    "responseMimeType": "application/json",
    "responseSchema": { "...": "see AI_SYSTEM.md section 5" },
    "temperature": 0.2
  }
}
```

**Response handling:**

1. If `promptFeedback.blockReason` is present, or there are no `candidates`, treat it as `blocked`.
2. If `candidates[0].finishReason` is not `STOP`, treat it as `incomplete`.
3. Parse `candidates[0].content.parts[0].text` as JSON and validate it against [AI_SYSTEM.md](AI_SYSTEM.md) §5.
4. Any failure produces the AI-unavailable path (FR-008). The raw text is never logged.

| Gemini response | MergeGuard behaviour |
| --- | --- |
| `400` | Log `ai.failed` reason `bad_request` (often an invalid model ID) |
| `401` / `403` | Log `ai.failed` reason `auth` |
| `429` | Log `ai.failed` reason `rate_limited` |
| `5xx` or timeout | Log `ai.failed` reason `unavailable` |

## 5. Outbound — AWS SSM Parameter Store

| Call | Parameters | Notes |
| --- | --- | --- |
| `GetParameters` | `Names = [<prefix>/github_token, <prefix>/webhook_secret, <prefix>/gemini_api_key]`, `WithDecryption = true` | One call per cold start or TTL expiry. Any entry in `InvalidParameters` means fail closed |

## 6. Internal — queue message (Phase 2)

**Status:** Proposed ([ADR-0005](ADR/0005-async-webhook-processing.md)). Contains metadata only; the worker fetches code itself.

```json
{
  "schema": "mergeguard.review-request.v1",
  "delivery_id": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
  "repository": "owner/repo",
  "pull_number": 7,
  "head_sha": "<40-hex>",
  "action": "synchronize",
  "received_at": "2026-10-21T08:00:00Z"
}
```

## 7. Versioning

- The inbound endpoint has a single unversioned path (`/webhook`) because GitHub is the only client. Breaking changes are made by deploying a new stack or route and repointing the webhook.
- The review marker `mergeguard:v1` and the queue schema `mergeguard.review-request.v1` are versioned. Bump them when their meaning changes, and keep reading the previous version for one release.
