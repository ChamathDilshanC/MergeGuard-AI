"""Queue message and sender for the receiver/worker split (FR-016, ADR-0005).

A message carries metadata only: delivery ID, repository, pull request number,
head SHA, action and receive time (docs/API_SPEC.md section 6). It never holds
code, titles or secrets, so the queue and its dead-letter queue hold nothing
sensitive. The worker fetches everything else itself.
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from mergeguard.models import PullRequestRef
from mergeguard.webhook import (
    REVIEWABLE_ACTIONS,
    is_valid_pull_number,
    is_valid_repo,
    is_valid_sha,
)

SCHEMA = "mergeguard.review-request.v1"
# Short deadlines: the receiver must acknowledge GitHub in well under 2 s.
SQS_CONNECT_TIMEOUT_SECONDS = 2
SQS_READ_TIMEOUT_SECONDS = 3
SQS_MAX_ATTEMPTS = 2

_DELIVERY_ID_RE = re.compile(r"[0-9A-Za-z-]{1,64}")


class QueueError(Exception):
    """The message could not be sent. Carries no AWS error text."""


class InvalidMessage(ValueError):
    """A queue message that is not a valid review request."""


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    delivery_id: str
    action: str
    pr: PullRequestRef


class SqsClient(Protocol):
    def send_message(self, *, QueueUrl: str, MessageBody: str) -> Mapping[str, Any]: ...


def _default_sqs_client() -> SqsClient:
    import boto3  # bundled with the Lambda runtime (ADR-0002)
    from botocore.config import Config

    return boto3.client(
        "sqs",
        config=Config(
            connect_timeout=SQS_CONNECT_TIMEOUT_SECONDS,
            read_timeout=SQS_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": SQS_MAX_ATTEMPTS, "mode": "standard"},
        ),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_message(
    delivery_id: str, action: str, pr: PullRequestRef, received_at: datetime
) -> str:
    return json.dumps(
        {
            "schema": SCHEMA,
            "delivery_id": delivery_id,
            "repository": pr.repo,
            "pull_number": pr.number,
            "head_sha": pr.head_sha,
            "action": action,
            "received_at": received_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        separators=(",", ":"),
    )


def parse_message(body: object) -> ReviewRequest:
    """Validate a message body as strictly as a webhook payload: the queue is
    trusted, but a malformed message must never reach an API URL."""
    try:
        data = json.loads(body) if isinstance(body, str) else None
    except ValueError as exc:
        raise InvalidMessage("not JSON") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise InvalidMessage("unsupported schema")
    delivery_id = data.get("delivery_id")
    action = data.get("action")
    valid = (
        isinstance(delivery_id, str)
        and _DELIVERY_ID_RE.fullmatch(delivery_id) is not None
        and isinstance(action, str)
        and action in REVIEWABLE_ACTIONS
        and is_valid_repo(data.get("repository"))
        and is_valid_pull_number(data.get("pull_number"))
        and is_valid_sha(data.get("head_sha"))
    )
    if not valid:
        raise InvalidMessage("invalid fields")
    return ReviewRequest(
        delivery_id=data["delivery_id"],
        action=data["action"],
        pr=PullRequestRef(
            repo=data["repository"],
            number=data["pull_number"],
            head_sha=data["head_sha"],
        ),
    )


class ReviewQueue:
    def __init__(
        self,
        url: str,
        client_factory: Callable[[], SqsClient] = _default_sqs_client,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.url = url
        self._client_factory = client_factory
        self._client: SqsClient | None = None
        self._clock = clock

    def enqueue(self, delivery_id: str, action: str, pr: PullRequestRef) -> None:
        body = build_message(delivery_id, action, pr, self._clock())
        try:
            if self._client is None:
                self._client = self._client_factory()
            self._client.send_message(QueueUrl=self.url, MessageBody=body)
        except Exception as exc:
            raise QueueError("could not send the review request") from exc
