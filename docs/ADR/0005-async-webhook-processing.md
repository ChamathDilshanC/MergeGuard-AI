# ADR-0005 — Asynchronous webhook processing

- **Status:** Accepted — implemented in M5 (T-501), tested with fakes, **not deployed**; the p95 acknowledgement time is measured in T-403
- **Date:** 2026-10-21
- **Related:** PRD FR-016, R-01 · [ARCHITECTURE.md](../ARCHITECTURE.md) §15 · [API_SPEC.md](../API_SPEC.md) §6

## Context

GitHub expects a webhook response within **10 seconds** and marks slower deliveries as failed. The brief's single function fetches files, calls Gemini, and posts the review before responding. AI latency alone can exceed 10 s, so deliveries show as failed even when a review is posted. That hides real failures among false ones. API Gateway HTTP APIs also cap integrations at 30 s.

## Decision

Split processing into two Lambda handlers that share one code package:

1. **Receiver:** verifies the signature, filters the event, and enqueues a **metadata-only** message (delivery ID, repository, PR number, head SHA, action) to an **Amazon SQS standard queue**. It responds `202`.
2. **Worker:** triggered by SQS. It fetches files, scans, calls Gemini, and posts the review. The worker timeout can exceed 30 s because API Gateway is no longer waiting.
3. A **dead-letter queue** receives messages after a fixed number of failed receives (3, `queue_max_receive_count`), with an alarm on its depth (T-506).
4. Duplicates from at-least-once delivery are absorbed by the stateless idempotency check (FR-014). Before posting, the worker skips the review if the PR head has moved past the queued SHA.

## Alternatives considered

| Option | Trade-off |
| --- | --- |
| Lambda asynchronous self-invocation (`InvocationType=Event`) | Fewer resources, with built-in retries and an on-failure destination; less visibility (no queue depth) and less control over retries |
| Step Functions | Strong orchestration and visibility; more cost and complexity than one linear pipeline needs |
| Keep synchronous and optimise | Cannot guarantee 10 s with an external AI call |

## Consequences

- Webhook deliveries become reliably green; real failures move to the DLQ and alarms.
- Two more AWS resources (queue, DLQ) and an event source mapping; IAM grows by `sqs:SendMessage` (receiver) and receive/delete permissions (worker). The two functions get **separate roles**, so the receiver reads only the webhook secret and the worker only the GitHub token and the Gemini key ([SECURITY.md](../SECURITY.md) §5.3).
- No code is stored at rest in SQS, because messages carry metadata only.
- Phase 1 stays synchronous, accepting R-01 for the MVP. The synchronous path is still in the code: with `MERGEGUARD_QUEUE_URL` unset the handler reviews inline, which tests and local runs use.

## Implementation notes (M5)

- The worker takes one message per invocation (`batch_size = 1`) and reports a failed one through `batchItemFailures`, so SQS retries only that message. It reports handled failures instead of crashing, so a failed review shows up in the dead-letter queue alarm, not in Lambda `Errors`.
- A message that finishes in any way (reviewed, skipped, duplicate, stale head, closed PR) is deleted. A message that fails is retried, then lands in the dead-letter queue, where the operator fixes the cause and redrives it ([DEPLOYMENT.md](../DEPLOYMENT.md) §10).
- The worker re-checks the pull request's head SHA and open state before it spends an AI call, and again before it posts (PRD EC-07).
- Worker concurrency is capped by the event source mapping (`worker_max_concurrency`), because reserved concurrency is not available on new accounts.
