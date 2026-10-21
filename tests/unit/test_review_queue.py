"""Queue message contract and sender (FR-016, API_SPEC section 6)."""

import json
from datetime import UTC, datetime

import pytest

from mergeguard.models import PullRequestRef
from mergeguard.review_queue import (
    SCHEMA,
    InvalidMessage,
    QueueError,
    ReviewQueue,
    build_message,
    parse_message,
)
from support import HEAD_SHA, QUEUE_URL, FakeSqs

NOW = datetime(2026, 10, 21, 8, 0, 0, tzinfo=UTC)
PR = PullRequestRef("owner/repo", 7, HEAD_SHA, author_login="octocat", draft=False)
DELIVERY = "72d3162e-cc78-11e3-81ab-4c9367dc0958"


def message(**overrides) -> str:
    data = json.loads(build_message(DELIVERY, "synchronize", PR, NOW))
    data.update(overrides)
    return json.dumps(data)


def test_message_matches_the_documented_shape():
    assert json.loads(build_message(DELIVERY, "synchronize", PR, NOW)) == {
        "schema": "mergeguard.review-request.v1",
        "delivery_id": DELIVERY,
        "repository": "owner/repo",
        "pull_number": 7,
        "head_sha": HEAD_SHA,
        "action": "synchronize",
        "received_at": "2026-10-21T08:00:00Z",
    }


def test_message_holds_metadata_only():
    """No code, no title, no author: the queue and the DLQ hold nothing sensitive."""
    body = build_message(DELIVERY, "opened", PR, NOW)
    assert "octocat" not in body
    assert set(json.loads(body)) == {
        "schema",
        "delivery_id",
        "repository",
        "pull_number",
        "head_sha",
        "action",
        "received_at",
    }


def test_round_trip():
    request = parse_message(build_message(DELIVERY, "reopened", PR, NOW))
    assert (request.delivery_id, request.action) == (DELIVERY, "reopened")
    assert (request.pr.repo, request.pr.number, request.pr.head_sha) == (
        "owner/repo",
        7,
        HEAD_SHA,
    )
    assert request.pr.short_sha == HEAD_SHA[:7]


@pytest.mark.parametrize(
    "body",
    [
        None,
        42,
        "not json",
        "[]",
        message(schema="mergeguard.review-request.v2"),
        message(delivery_id="<script>"),
        message(delivery_id=7),
        message(repository="owner/../repo"),
        message(repository="owner"),
        message(pull_number=0),
        message(pull_number=True),
        message(pull_number="7"),
        message(head_sha="abc"),
        message(action="closed"),
        message(action=None),
    ],
)
def test_invalid_messages_are_rejected(body):
    with pytest.raises(InvalidMessage):
        parse_message(body)


def test_enqueue_sends_one_message_to_the_configured_queue():
    sqs = FakeSqs()
    ReviewQueue(QUEUE_URL, lambda: sqs, clock=lambda: NOW).enqueue(
        DELIVERY, "opened", PR
    )
    [sent] = sqs.sent
    assert sent["QueueUrl"] == QUEUE_URL
    assert parse_message(sent["MessageBody"]).pr.number == 7


def test_the_client_is_created_once():
    created = []

    def factory() -> FakeSqs:
        created.append(1)
        return FakeSqs()

    queue = ReviewQueue(QUEUE_URL, factory)
    queue.enqueue(DELIVERY, "opened", PR)
    queue.enqueue(DELIVERY, "opened", PR)
    assert created == [1]


def test_send_failures_become_a_queue_error_without_aws_text():
    sqs = FakeSqs(
        error=RuntimeError("AccessDenied: arn:aws:sqs:us-east-1:123:secret-q")
    )
    with pytest.raises(QueueError) as raised:
        ReviewQueue(QUEUE_URL, lambda: sqs).enqueue(DELIVERY, "opened", PR)
    assert "AccessDenied" not in str(raised.value)


def test_default_client_has_short_timeouts(monkeypatch):
    """boto3 and botocore are bundled with Lambda, not installed here (ADR-0002),
    so both are replaced by recording fakes."""
    import sys
    import types

    seen = {}

    class Config:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    def client(service, config=None):
        seen["service"], seen["config"] = service, config
        return object()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(
        sys.modules, "botocore.config", types.SimpleNamespace(Config=Config)
    )
    from mergeguard.review_queue import _default_sqs_client

    _default_sqs_client()
    assert seen["service"] == "sqs"
    assert seen["config"].connect_timeout == 2 and seen["config"].read_timeout == 3
    assert seen["config"].retries == {"max_attempts": 2, "mode": "standard"}


def test_schema_constant_is_the_documented_one():
    assert SCHEMA == "mergeguard.review-request.v1"
