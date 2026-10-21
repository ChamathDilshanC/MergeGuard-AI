"""The optional installation ID of a queue message (FR-020, API_SPEC section 6)."""

import json
from datetime import UTC, datetime

import pytest

from mergeguard.models import PullRequestRef
from mergeguard.review_queue import InvalidMessage, build_message, parse_message
from support import HEAD_SHA

NOW = datetime(2026, 10, 21, 8, 0, 0, tzinfo=UTC)
DELIVERY = "72d3162e-cc78-11e3-81ab-4c9367dc0958"


def message(install=None, **overrides) -> str:
    pr = PullRequestRef("owner/repo", 7, HEAD_SHA, installation_id=install)
    data = json.loads(build_message(DELIVERY, "opened", pr, NOW))
    data.update(overrides)
    return json.dumps(data)


def test_the_installation_id_round_trips():
    request = parse_message(message(42))
    assert request.pr.installation_id == 42


def test_a_personal_token_message_has_no_installation_id_at_all():
    assert "installation_id" not in json.loads(message())
    assert parse_message(message()).pr.installation_id is None


def test_the_message_stays_schema_v1_with_the_optional_field():
    assert json.loads(message(42))["schema"] == "mergeguard.review-request.v1"


@pytest.mark.parametrize("value", [0, -1, "42", None, True, 1.5, [42]])
def test_an_invalid_installation_id_is_rejected(value):
    with pytest.raises(InvalidMessage):
        parse_message(message(7, installation_id=value))
