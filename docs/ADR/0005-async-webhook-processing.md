# ADR-0005 — Asynchronous webhook processing

- **Status:** Proposed — target Phase 2
- **Date:** 2026-10-21
- **Related:** PRD FR-016, R-01 · [ARCHITECTURE.md](../ARCHITECTURE.md) §15 · [API_SPEC.md](../API_SPEC.md) §6

## Context

GitHub expects a webhook response within **10 seconds** and marks slower deliveries as failed. The brief's single function fetches files, calls Gemini, and posts the review before responding. AI latency alone can exceed 10 s, so deliveries show as failed even when a review is posted. That hides real failures among false ones. API Gateway HTTP APIs also cap integrations at 30 s.

## Decision (proposed)

Split processing into two Lambda handlers that share one code package:

1. **Receiver:** verifies the signature, filters the event, and enqueues a **metadata-only** message (delivery ID, repository, PR number, head SHA, action) to an **Amazon SQS standard queue**. It responds `202`.
2. **Worker:** triggered by SQS. It fetches files, scans, calls Gemini, and posts the review. The worker timeout can exceed 30 s because API Gateway is no longer waiting.
3. A **dead-letter queue** receives messages after a fixed number of failed receives (Proposed: 3), with an alarm on its depth.
4. Duplicates from at-least-once delivery are absorbed by the stateless idempotency check (FR-014). Before posting, the worker skips the review if the PR head has moved past the queued SHA.

## Alternatives considered

| Option | Trade-off |
| --- | --- |
| Lambda asynchronous self-invocation (`InvocationType=Event`) | Fewer resources, with built-in retries and an on-failure destination; less visibility (no queue depth) and less control over retries |
| Step Functions | Strong orchestration and visibility; more cost and complexity than one linear pipeline needs |
| Keep synchronous and optimise | Cannot guarantee 10 s with an external AI call |

## Consequences

- Webhook deliveries become reliably green; real failures move to the DLQ and alarms.
- Two more AWS resources (queue, DLQ) and an event source mapping; IAM grows by `sqs:SendMessage` (receiver) and receive/delete permissions (worker).
- No code is stored at rest in SQS, because messages carry metadata only.
- Phase 1 stays synchronous, accepting R-01 for the MVP.
