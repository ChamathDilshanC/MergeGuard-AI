# ADR-0001 — Serverless webhook architecture on API Gateway HTTP API and Lambda

- **Status:** Accepted (brief) — not yet implemented
- **Date:** 2026-10-21
- **Related:** [ARCHITECTURE.md](../ARCHITECTURE.md) §5 · PRD FR-001, FR-012

## Context

MergeGuard reacts to GitHub pull-request events. Traffic is bursty and low volume (one event per PR open or push). The owner's goals are zero servers to operate, minimal cost, and a deployment that takes minutes.

## Decision

- Receive webhooks on an **Amazon API Gateway HTTP API**: `$default` stage with auto-deploy and a single route, `POST /webhook`.
- Use an `AWS_PROXY` integration with payload format `2.0` to one **AWS Lambda** function, `mergeguard-ai-pr-reviewer` (Python 3.12, 256 MB, 30 s).
- Provision everything with **Terraform**. Its output `github_webhook_url` is pasted into GitHub.

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| GitHub Actions workflow on `pull_request` | Runs in each repository's CI and needs workflow files and secrets per repository; forked PRs cannot access secrets by default |
| Lambda Function URL (no API Gateway) | Viable and simpler, but no built-in throttling. API Gateway also lets routes be added later without changing the URL scheme |
| API Gateway REST API | More features (resource policies, WAF), but more resources and higher cost; not needed for one route |
| Container or VM service | Contradicts the zero-ops goal |

## Consequences

- No servers, patching, or idle cost.
- An HTTP API integration times out after at most 30 s, which caps synchronous processing (see [ADR-0005](0005-async-webhook-processing.md)).
- The endpoint is public, so authentication relies on webhook signatures (PRD FR-002, [SECURITY.md](../SECURITY.md) TM-01), and abuse protection relies on throttling (TM-08).
