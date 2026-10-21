"""AWS Lambda entry point (handler: ``lambda_function.lambda_handler``).

Translates the API Gateway payload v2.0 event into a pipeline call and the
result into an HTTP response (docs/API_SPEC.md section 2.3). No review logic
lives here.
"""

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from mergeguard.config import ConfigError, SecretStore, Settings
from mergeguard.github_client import GitHubClient, GitHubError
from mergeguard.http import Transport, urllib_transport
from mergeguard.logging_util import JsonLogger
from mergeguard.pipeline import Outcome, ReviewPipeline
from mergeguard.webhook import (
    InvalidBody,
    MalformedPayload,
    lower_headers,
    raw_body,
    triage,
    verify_signature,
)

# Seconds kept free after a GitHub retry so the response is still returned.
TIME_SAFETY_MARGIN_SECONDS = 2.0

_DELIVERY_ID_RE = re.compile(r"[0-9A-Za-z-]{1,64}")
_EVENT_NAME_RE = re.compile(r"[a-z_]{1,40}")

_secret_store: SecretStore | None = None


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    return handle(
        event,
        context,
        environ=os.environ,
        secrets=_cached_secret_store,
        transport=urllib_transport,
    )


def _cached_secret_store(prefix: str) -> SecretStore:
    """One store per execution environment, so the SSM cache survives
    invocations."""
    global _secret_store
    if _secret_store is None or _secret_store.prefix != prefix:
        _secret_store = SecretStore(prefix)
    return _secret_store


def handle(
    event: Mapping[str, Any],
    context: Any,
    *,
    environ: Mapping[str, str],
    secrets: Callable[[str], SecretStore],
    transport: Transport,
    stream: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
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
            event, context, headers, event_name, settings, secrets, transport, log
        )
    except ConfigError:
        log.log("config.failed", "ERROR")
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
    settings: Settings,
    secrets: Callable[[str], SecretStore],
    transport: Transport,
    log: JsonLogger,
) -> Outcome:
    credentials = secrets(settings.param_prefix).get()  # fails closed (FR-011)
    try:
        body = raw_body(event)
        authentic = verify_signature(
            credentials.webhook_secret, body, headers.get("x-hub-signature-256")
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
    github = GitHubClient(
        credentials.github_token, transport, has_time=_time_budget(context)
    )
    pipeline = ReviewPipeline(
        github, pr_log, max_files=settings.max_files, dry_run=settings.dry_run
    )
    return pipeline.run(pr)


def _time_budget(context: Any) -> Callable[[float], bool]:
    remaining = getattr(context, "get_remaining_time_in_millis", None)

    def has_time(seconds: float) -> bool:
        if remaining is None:
            return True
        return remaining() / 1000 > seconds + TIME_SAFETY_MARGIN_SECONDS

    return has_time


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
