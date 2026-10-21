"""AWS Lambda entry point for the SQS worker (handler:
``worker_function.lambda_handler``), the second half of the receiver/worker
split (FR-016, ADR-0005).

Each SQS record is one review request. A request that finishes in any way (a
review, a skip, a duplicate, a stale head) is deleted from the queue. One that
fails is reported through ``batchItemFailures``, so SQS retries it and, after
the redrive limit, moves it to the dead-letter queue, where an alarm raises it.
"""

import os
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from mergeguard.config import ConfigError, SecretStore, Settings
from mergeguard.github_client import GitHubError
from mergeguard.http import Transport, urllib_transport
from mergeguard.logging_util import JsonLogger
from mergeguard.review_queue import InvalidMessage, parse_message
from mergeguard.runtime import build_pipeline

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
    records = event.get("Records")
    records = [item for item in records if isinstance(item, dict)] if records else []
    log = JsonLogger("INFO", stream)
    try:
        settings = Settings.from_env(environ)
    except ConfigError:
        log.log("config.failed", "ERROR")
        return _failures(records)  # every record is retried until it is fixed
    log = JsonLogger(settings.log_level, stream)
    failed = [
        record
        for record in records
        if not _process(record, context, settings, secrets, transport, log, clock)
    ]
    return _failures(failed)


def _process(
    record: Mapping[str, Any],
    context: Any,
    settings: Settings,
    secrets: Callable[[str], SecretStore],
    transport: Transport,
    log: JsonLogger,
    clock: Callable[[], float],
) -> bool:
    """Handle one record. True means done (delete it); False means retry."""
    started = clock()
    try:
        request = parse_message(record.get("body"))
    except InvalidMessage:
        log.log("worker.rejected", "ERROR", reason="malformed_message")
        return False  # a bad message ends up in the DLQ, where it is visible
    pr = request.pr
    log = log.bind(
        delivery_id=request.delivery_id,
        repo=pr.repo,
        pr=pr.number,
        head_sha=pr.short_sha,
        action=request.action,
    )
    log.log("worker.received")
    try:
        store = secrets(settings.param_prefix)
        pipeline = build_pipeline(
            settings,
            github_token=store.github_token(),  # fails closed (FR-011)
            gemini_api_key=store.gemini_api_key,
            transport=transport,
            context=context,
            logger=log,
            verify_head=True,
        )
        outcome = pipeline.run(pr)
    except ConfigError:
        log.log("config.failed", "ERROR")
        return False
    except GitHubError as exc:
        log.log(f"github.{exc.kind}", "ERROR", http_status=exc.status)
        return False
    except Exception as exc:
        log.log("error.unhandled", "ERROR", error_type=type(exc).__name__)
        return False
    log.log(
        "worker.completed",
        status=outcome.status,
        reason=outcome.reason,
        duration_ms=round((clock() - started) * 1000),
    )
    return True


def _failures(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "batchItemFailures": [
            {"itemIdentifier": record["messageId"]}
            for record in records
            if isinstance(record.get("messageId"), str)
        ]
    }
