"""AWS Lambda entry point (handler: ``lambda_function.lambda_handler``).

Translates the API Gateway payload v2.0 event into a pipeline call and the
result into an HTTP response (docs/API_SPEC.md section 2.3). No review logic
lives here.

With ``MERGEGUARD_QUEUE_URL`` set this is the Phase 2 receiver (FR-016): it
verifies the signature, filters the event, queues a metadata-only message and
answers ``202``. The SQS worker in ``worker_function`` does the review. Without
it the review runs inline (Phase 1), which tests and local runs rely on.
"""

import json
import os
import re
import time
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any, TextIO

from mergeguard.config import ConfigError, SecretStore, Settings
from mergeguard.github_client import GitHubError
from mergeguard.http import Transport, urllib_transport
from mergeguard.logging_util import JsonLogger
from mergeguard.pipeline import Outcome
from mergeguard.review_queue import QueueError, ReviewQueue
from mergeguard.runtime import build_pipeline, github_identity
from mergeguard.webhook import (
    InvalidBody,
    MalformedPayload,
    lower_headers,
    raw_body,
    triage,
    verify_signature,
)

_DELIVERY_ID_RE = re.compile(r"[0-9A-Za-z-]{1,64}")
_EVENT_NAME_RE = re.compile(r"[a-z_]{1,40}")

_secret_store: SecretStore | None = None
_review_queue: ReviewQueue | None = None
_app_cache: dict[Any, Any] = {}  # installation tokens, per execution environment


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    return handle(
        event,
        context,
        environ=os.environ,
        secrets=_cached_secret_store,
        transport=urllib_transport,
        queue=_cached_review_queue,
        app_cache=_app_cache,
    )


def _cached_secret_store(prefix: str) -> SecretStore:
    """One store per execution environment, so the SSM cache survives
    invocations."""
    global _secret_store
    if _secret_store is None or _secret_store.prefix != prefix:
        _secret_store = SecretStore(prefix)
    return _secret_store


def _cached_review_queue(url: str) -> ReviewQueue:
    global _review_queue
    if _review_queue is None or _review_queue.url != url:
        _review_queue = ReviewQueue(url)
    return _review_queue


def handle(
    event: Mapping[str, Any],
    context: Any,
    *,
    environ: Mapping[str, str],
    secrets: Callable[[str], SecretStore],
    transport: Transport,
    queue: Callable[[str], ReviewQueue] | None = None,
    stream: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    app_cache: MutableMapping[Any, Any] | None = None,
) -> dict[str, Any]:
    started = clock()
    headers = lower_headers(event)
    # Headers are attacker-controlled until the signature is verified.
    delivery_id = _safe(headers.get("x-github-delivery", ""), _DELIVERY_ID_RE)
    event_name = _safe(headers.get("x-github-event", ""), _EVENT_NAME_RE)
    log = JsonLogger("INFO", stream, delivery_id=delivery_id)
    try:
        settings = Settings.from_env(environ)
        log = JsonLogger(settings.log_level, stream, delivery_id=delivery_id)
        log.log("webhook.received", github_event=event_name)
        outcome = _process(
            event,
            context,
            headers,
            event_name,
            delivery_id,
            settings,
            secrets,
            transport,
            queue,
            log,
            app_cache,
        )
    except ConfigError:
        log.log("config.failed", "ERROR")
        outcome = Outcome(500, "error", "internal")
    except QueueError:
        log.log("queue.failed", "ERROR")
        outcome = Outcome(500, "error", "internal")
    except GitHubError as exc:
        log.log(f"github.{exc.kind}", "ERROR", http_status=exc.status)
        outcome = Outcome(500, "error", "internal")
    except Exception as exc:
        log.log("error.unhandled", "ERROR", error_type=type(exc).__name__)
        outcome = Outcome(500, "error", "internal")
    log.log(
        "webhook.completed",
        status=outcome.status,
        reason=outcome.reason,
        http_status=outcome.http_status,
        duration_ms=round((clock() - started) * 1000),
    )
    return _response(outcome, delivery_id)


def _process(
    event: Mapping[str, Any],
    context: Any,
    headers: Mapping[str, str],
    event_name: str,
    delivery_id: str,
    settings: Settings,
    secrets: Callable[[str], SecretStore],
    transport: Transport,
    queue: Callable[[str], ReviewQueue] | None,
    log: JsonLogger,
    app_cache: MutableMapping[Any, Any] | None,
) -> Outcome:
    store = secrets(settings.param_prefix)
    receiver_queue = None
    credentials = None
    if settings.queue_url is not None:
        if queue is None:
            raise ConfigError("MERGEGUARD_QUEUE_URL is set but no queue is wired")
        receiver_queue = queue(settings.queue_url)
        # The receiver reads only the webhook secret; the token stays with the
        # worker (least privilege, SECURITY.md section 5.3). Fails closed.
        webhook_secret = store.webhook_secret()
    elif settings.github_app_id is not None:
        webhook_secret = store.webhook_secret()  # App mode never reads the PAT
    else:
        credentials = store.get()  # fails closed (FR-011)
        webhook_secret = credentials.webhook_secret
    try:
        body = raw_body(event)
        authentic = verify_signature(
            webhook_secret, body, headers.get("x-hub-signature-256")
        )
    except InvalidBody:
        authentic = False
    if not authentic:
        log.log("webhook.rejected", reason="invalid_signature")
        return Outcome(401, "rejected", "invalid_signature")

    try:
        triaged = triage(event_name, body)
    except MalformedPayload as exc:
        log.log("webhook.rejected", reason=exc.reason)
        return Outcome(400, "rejected", exc.reason)
    if triaged.status == "pong":
        return Outcome(200, "pong")
    if triaged.pull_request is None or triaged.status == "ignored":
        log.log("webhook.ignored", reason=triaged.reason, action=triaged.action)
        return Outcome(200, "ignored", triaged.reason)

    pr = triaged.pull_request
    pr_log = log.bind(
        repo=pr.repo, pr=pr.number, head_sha=pr.short_sha, action=triaged.action
    )
    if receiver_queue is not None:
        receiver_queue.enqueue(delivery_id, triaged.action or "", pr)
        pr_log.log("queue.enqueued")
        return Outcome(202, "accepted")
    github_token, login = github_identity(
        settings,
        store,
        transport,
        pr,
        token=credentials.github_token if credentials else None,
        cache=app_cache,
    )
    pipeline = build_pipeline(
        settings,
        github_token=github_token,
        login=login,
        gemini_api_key=store.gemini_api_key,
        transport=transport,
        context=context,
        logger=pr_log,
        verify_head=False,
    )
    return pipeline.run(pr)


def _safe(value: str, pattern: re.Pattern[str]) -> str:
    return value if pattern.fullmatch(value) else "invalid"


def _response(outcome: Outcome, delivery_id: str) -> dict[str, Any]:
    payload: dict[str, str] = {"status": outcome.status}
    if outcome.reason:
        payload["reason"] = outcome.reason
    payload["delivery_id"] = delivery_id
    return {
        "statusCode": outcome.http_status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }
