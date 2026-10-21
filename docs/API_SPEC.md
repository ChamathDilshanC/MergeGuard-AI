# MergeGuard AI — API Specification

| Field | Value |
| --- | --- |
| Status | Draft v0.1 — inbound contract (§2), GitHub and SSM calls (§3, §5) implemented in M2; Gemini (§4) implemented in M3 and tested only against mocked responses; **nothing is deployed yet** |
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
| Valid signature, JSON is not a usable `pull_request` payload (missing fields, invalid repository name or SHA) | `400` | `rejected` | `malformed_payload` |
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

`delivery_id` echoes `X-GitHub-Delivery` only if it looks like a delivery GUID (letters, digits and `-`, at most 64 characters). Otherwise it is `invalid`, because the header is attacker-controlled on unsigned requests.

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
| List reviews (duplicate check) | `GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews?per_page=100&page={n}` | 1 | Up to 10 pages. A duplicate needs matching `commit_id`, the `mergeguard:v1` marker in `body`, **and** `user.login` equal to the token owner |
| Identify the token owner | `GET /user` | 1 | Cached per client. Used so a copied marker in someone else's review cannot suppress MergeGuard ([SECURITY.md](SECURITY.md) TM-14) |
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
| `5xx`, or no response (DNS, TLS, timeout) | GitHub outage or network failure | `GET`: one retry if time allows, otherwise `500`. `POST`: no retry (the review may already exist); log `github.unavailable`; `500` |
| Any other status, or a body that is not the expected JSON | Unexpected response | Log `github.unexpected`; respond `500` |

### 3.3 Token permissions (fine-grained PAT, Phase 1)

| Permission | Access | Why |
| --- | --- | --- |
| Repository access | Only selected repositories | Least privilege |
| Pull requests | Read and write | List files and reviews; create and dismiss reviews |
| Contents | Read | Listed in the brief; keep until tests confirm it is not needed |
| Metadata | Read (always included) | Required by GitHub |
| Commit statuses | Read and write | Phase 2 (FR-017) |

## 4. Outbound — Google Gemini API

**Status:** Implemented in M3, with the key moved into a header (RB-07). Tested only against mocked responses; no real call has been made yet (OQ-05).

| Item | Value |
| --- | --- |
| Method and URL | `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` |
| `{model}` | `MERGEGUARD_GEMINI_MODEL`, default `gemini-2.5-flash` |
| Auth header | `x-goog-api-key: <gemini_api_key from SSM>` — never in the query string |
| Timeout | Derived from the remaining Lambda time, reserving ≥ 5 s for posting; at most 20 s; no call if less than 3 s would remain |
| Tools / function calling | None; the request has no `tools` field |

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
3. Join the `text` of `candidates[0].content.parts` (parts marked `thought` are skipped), parse it as JSON and validate it against [AI_SYSTEM.md](AI_SYSTEM.md) §5.
4. Any failure produces the AI-unavailable path (FR-008). The raw text is never logged.

| Gemini response | MergeGuard behaviour |
| --- | --- |
| `400` / `404` | Log `ai.failed` reason `bad_request` (often an invalid model ID) |
| `401` / `403` | Log `ai.failed` reason `auth` |
| `429` | Log `ai.failed` reason `rate_limited` |
| `5xx` or network failure | Log `ai.failed` reason `unavailable` |
| Timeout | Log `ai.failed` reason `timeout` |

## 5. Outbound — AWS SSM Parameter Store

| Call | Parameters | Notes |
| --- | --- | --- |
| `GetParameters` (core) | `Names = [<prefix>/github_token, <prefix>/webhook_secret]`, `WithDecryption = true` | One call per cold start or 15-minute TTL expiry. Any entry in `InvalidParameters`, an empty value, or the Terraform placeholder (ADR-0007) means fail closed (`500`) |
| `GetParameters` (Gemini key) | `Names = [<prefix>/gemini_api_key]`, `WithDecryption = true` | Only when an AI review runs; cached the same way. A missing, empty or placeholder key gives the AI-unavailable comment with reason `not_configured`, not a `500`, and no diff is sent |

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
