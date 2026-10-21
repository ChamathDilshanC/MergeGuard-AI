"""Signature verification (ST-01 to ST-04) and event triage (FR-002, FR-003)."""

import base64
import json

import pytest

from mergeguard.webhook import (
    InvalidBody,
    MalformedPayload,
    lower_headers,
    parse_pull_request,
    raw_body,
    triage,
    verify_signature,
)
from support import TEST_WEBHOOK_SECRET, load_fixture, pull_request_payload, sign

BODY = json.dumps(pull_request_payload()).encode()


# --- signature (docs/TESTING_STRATEGY.md section 6) --------------------------


def test_st01_valid_signature_is_accepted():
    assert verify_signature(TEST_WEBHOOK_SECRET, BODY, sign(BODY)) is True


def test_st02_tampered_body_with_original_signature_is_rejected():
    tampered = BODY.replace(b'"opened"', b'"closed"')
    assert tampered != BODY
    assert verify_signature(TEST_WEBHOOK_SECRET, tampered, sign(BODY)) is False


def test_st02_reformatted_json_is_rejected():
    # The HMAC covers the exact bytes, not the parsed JSON.
    reformatted = json.dumps(json.loads(BODY), indent=2).encode()
    assert verify_signature(TEST_WEBHOOK_SECRET, reformatted, sign(BODY)) is False


@pytest.mark.parametrize(
    "header",
    [
        None,  # missing
        "",  # empty
        "sha1=" + "0" * 40,  # wrong algorithm prefix
        "sha256=",  # prefix only
        "sha256=" + "0" * 64,  # wrong digest
        "sha256=" + "0" * 10,  # wrong length
        "sha256=é" + "0" * 63,  # non-ASCII must not crash compare_digest
    ],
    ids=["missing", "empty", "sha1", "prefix-only", "wrong", "short", "non-ascii"],
)
def test_st03_missing_or_malformed_header_is_rejected(header):
    assert verify_signature(TEST_WEBHOOK_SECRET, BODY, header) is False


def test_signature_with_another_secret_is_rejected():
    assert verify_signature(TEST_WEBHOOK_SECRET, BODY, sign(BODY, "other")) is False


def test_empty_secret_fails_closed():
    with pytest.raises(ValueError):
        verify_signature("", BODY, sign(BODY))


def test_st04_base64_body_is_decoded_before_verification():
    event = {"body": base64.b64encode(BODY).decode(), "isBase64Encoded": True}
    body = raw_body(event)
    assert body == BODY
    assert verify_signature(TEST_WEBHOOK_SECRET, body, sign(BODY)) is True


def test_invalid_base64_body_is_rejected():
    with pytest.raises(InvalidBody):
        raw_body({"body": "not base64!", "isBase64Encoded": True})


def test_plain_body_keeps_exact_utf8_bytes():
    text = '{"title": "café ✓"}'
    assert raw_body({"body": text, "isBase64Encoded": False}) == text.encode("utf-8")
    assert raw_body({}) == b""


def test_headers_are_looked_up_case_insensitively():
    event = {"headers": {"X-GitHub-Event": "ping", "X-Hub-Signature-256": "x"}}
    assert lower_headers(event) == {
        "x-github-event": "ping",
        "x-hub-signature-256": "x",
    }
    assert lower_headers({"headers": None}) == {}


# --- triage (FR-003) ------------------------------------------------------------


def _body(payload) -> bytes:
    return json.dumps(payload).encode()


def test_ping_is_answered_without_parsing():
    assert triage("ping", b"not json").status == "pong"


def test_other_events_are_ignored_even_with_an_opened_action():
    """RB-04: the brief only checked ``action`` and crashed on issue events."""
    result = triage("issues", _body(load_fixture("webhooks/issues_opened.json")))
    assert (result.status, result.reason) == ("ignored", "event_not_supported")


@pytest.mark.parametrize(
    "action", ["opened", "synchronize", "reopened", "ready_for_review"]
)
def test_reviewable_actions(action):
    result = triage("pull_request", _body(pull_request_payload(action=action)))
    assert result.status == "review"
    assert result.action == action
    assert result.pull_request is not None
    assert result.pull_request.repo == "owner/repo"
    assert result.pull_request.number == 7
    assert result.pull_request.short_sha == "0123456"
    assert result.pull_request.author_login == "octocat"


@pytest.mark.parametrize("action", ["closed", "edited", "labeled", None, ["opened"]])
def test_other_actions_are_ignored(action):
    result = triage("pull_request", _body(pull_request_payload(action=action)))
    assert (result.status, result.reason) == ("ignored", "action_not_supported")


def test_draft_pull_requests_are_skipped():
    payload = pull_request_payload(pull_request__draft=True)
    result = triage("pull_request", _body(payload))
    assert (result.status, result.reason) == ("ignored", "draft")


def test_malformed_json_is_reported():
    with pytest.raises(MalformedPayload) as caught:
        triage("pull_request", b"{not json")
    assert caught.value.reason == "malformed_json"


def test_non_object_json_is_reported():
    with pytest.raises(MalformedPayload) as caught:
        triage("pull_request", b"[1, 2]")
    assert caught.value.reason == "malformed_payload"


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository__full_name": "no-slash"},
        {"repository__full_name": "owner/.."},
        {"repository__full_name": "owner/repo/extra"},
        {"pull_request__number": True},
        {"pull_request__number": 0},
        {"pull_request__head": {"sha": "not-a-sha"}},
        {"pull_request__draft": "no"},
        {"repository": None},
    ],
    ids=[
        "no-slash",
        "dot-dot",
        "extra-path",
        "bool-number",
        "zero",
        "sha",
        "draft",
        "repo",
    ],
)
def test_invalid_pull_request_fields_are_rejected(overrides):
    with pytest.raises(MalformedPayload) as caught:
        parse_pull_request(pull_request_payload(**overrides))
    assert caught.value.reason == "malformed_payload"


def test_missing_author_login_is_tolerated():
    pr = parse_pull_request(pull_request_payload(pull_request__user={}))
    assert pr.author_login == ""
