"""Handler end to end with a fake GitHub API and a fake SSM client.

Covers T-201, T-207 to T-210 and the security tests ST-02 to ST-06 at the HTTP
boundary (docs/TESTING_STRATEGY.md sections 6 and 8).
"""

import io
import json

import pytest

import lambda_function
from mergeguard.config import SecretStore
from mergeguard.http import TransportError
from mergeguard.logging_util import ALLOWED_FIELDS
from mergeguard.render import FALLBACK_CALLOUT, MARKER_PREFIX
from support import (
    AWS_EXAMPLE_KEY_ID,
    BOT_LOGIN,
    HEAD_SHA,
    TEST_GITHUB_TOKEN,
    TEST_WEBHOOK_SECRET,
    FakeSsm,
    FakeTransport,
    api_event,
    file_entry,
    github_api,
    json_response,
    load_fixture,
    pull_request_payload,
    sign,
)

LEAKY_PATCH = f"@@ -0,0 +1,2 @@\n+import os\n+AWS_KEY = '{AWS_EXAMPLE_KEY_ID}'"
CLEAN_PATCH = "@@ -1 +1 @@\n-x = 1\n+x = 2"
REVIEWS_POST = r"/pulls/7/reviews$"


class Harness:
    def __init__(self, transport: FakeTransport, env: dict | None = None, ssm=None):
        self.transport = transport
        self.env = env or {}
        self.ssm = ssm or FakeSsm()
        self.logs = io.StringIO()

    def __call__(self, event: dict, context=None) -> tuple[int, dict]:
        store = SecretStore("/mergeguard", lambda: self.ssm)
        response = lambda_function.handle(
            event,
            context,
            environ=self.env,
            secrets=lambda prefix: store,
            transport=self.transport,
            stream=self.logs,
        )
        assert response["headers"] == {"Content-Type": "application/json"}
        return response["statusCode"], json.loads(response["body"])

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.logs.getvalue().splitlines()]

    def events(self) -> list[str]:
        return [record["event"] for record in self.records()]


def posted_reviews(transport: FakeTransport) -> list[dict]:
    return [call.json() for call in transport.requests("POST", REVIEWS_POST)]


# --- webhook boundary ------------------------------------------------------------


def test_ping_is_answered_with_pong():
    harness = Harness(FakeTransport())
    status, body = harness(api_event(load_fixture("webhooks/ping.json"), event="ping"))
    assert (status, body["status"]) == (200, "pong")
    assert body["delivery_id"] == "72d3162e-cc78-11e3-81ab-4c9367dc0958"
    assert harness.transport.calls == []


def test_st02_tampered_body_is_rejected_before_any_call():
    event = api_event(pull_request_payload())
    event["body"] = event["body"].replace("opened", "closed")
    harness = Harness(FakeTransport())
    assert harness(event) == (
        401,
        {
            "status": "rejected",
            "reason": "invalid_signature",
            "delivery_id": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
        },
    )
    assert harness.transport.calls == []


@pytest.mark.parametrize(
    "signature", [None, "", "sha1=abc"], ids=["missing", "empty", "sha1"]
)
def test_st03_missing_or_wrong_signature_is_rejected(signature):
    harness = Harness(FakeTransport())
    status, body = harness(api_event(pull_request_payload(), signature=signature))
    assert (status, body["reason"]) == (401, "invalid_signature")
    assert harness.transport.calls == []


def test_st04_base64_encoded_body_is_processed():
    harness = Harness(github_api(files=[file_entry("a.py", CLEAN_PATCH)]))
    status, body = harness(api_event(pull_request_payload(), base64_body=True))
    assert (status, body["status"]) == (200, "skipped")


def test_invalid_base64_body_is_rejected():
    event = api_event(pull_request_payload(), base64_body=True)
    event["body"] = "%%%"
    assert Harness(FakeTransport())(event)[0] == 401


def test_rb04_other_events_are_ignored():
    harness = Harness(FakeTransport())
    status, body = harness(
        api_event(load_fixture("webhooks/issues_opened.json"), event="issues")
    )
    assert (status, body["status"], body["reason"]) == (
        200,
        "ignored",
        "event_not_supported",
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"action": "closed"}, "action_not_supported"),
        ({"pull_request__draft": True}, "draft"),
    ],
)
def test_unreviewable_pull_requests_are_ignored(overrides, reason):
    harness = Harness(FakeTransport())
    status, body = harness(api_event(pull_request_payload(**overrides)))
    assert (status, body["status"], body["reason"]) == (200, "ignored", reason)
    assert harness.transport.calls == []


@pytest.mark.parametrize(
    ("raw", "reason"),
    [(b"{not json", "malformed_json"), (b"[]", "malformed_payload")],
)
def test_malformed_payloads_are_rejected_with_400(raw, reason):
    status, body = Harness(FakeTransport())(api_event(raw))
    assert (status, body["status"], body["reason"]) == (400, "rejected", reason)


def test_untrusted_header_values_are_not_echoed():
    event = api_event(pull_request_payload(), signature=None, delivery_id="<script>")
    status, body = Harness(FakeTransport())(event)
    assert (status, body["delivery_id"]) == (401, "invalid")


# --- review pipeline -------------------------------------------------------------


def test_secret_in_pr_posts_request_changes_without_the_value():
    transport = github_api(files=[file_entry("settings.py", LEAKY_PATCH)])
    harness = Harness(transport)
    status, body = harness(api_event(pull_request_payload()))
    assert (status, body["status"], body["reason"]) == (
        200,
        "reviewed",
        "REQUEST_CHANGES",
    )
    [review] = posted_reviews(transport)
    assert review["event"] == "REQUEST_CHANGES"
    assert review["commit_id"] == HEAD_SHA
    assert review["body"].startswith(MARKER_PREFIX + "kind=security sha=0123456")
    assert "| 1 | AWS access key ID | `settings.py` | 2 |" in review["body"]
    assert AWS_EXAMPLE_KEY_ID not in review["body"]


def test_st05_secret_findings_make_no_ai_or_non_github_calls():
    transport = github_api(files=[file_entry("settings.py", LEAKY_PATCH)])
    Harness(transport)(api_event(pull_request_payload()))
    assert transport.calls
    assert all(c.url.startswith("https://api.github.com/") for c in transport.calls)
    assert not any("generativelanguage" in c.url for c in transport.calls)


def test_own_pr_refusal_falls_back_to_a_comment():
    transport = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)],
        review_responses=(json_response(422, {}), json_response(200, {})),
    )
    harness = Harness(transport)
    status, body = harness(api_event(pull_request_payload()))
    assert (status, body["reason"]) == (200, "COMMENT")
    first, second = posted_reviews(transport)
    assert first["event"] == "REQUEST_CHANGES" and FALLBACK_CALLOUT not in first["body"]
    assert second["event"] == "COMMENT" and FALLBACK_CALLOUT in second["body"]
    assert "review.fallback_comment" in harness.events()


def test_second_refusal_is_reported_as_an_error():
    transport = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)],
        review_responses=(json_response(422, {}),),
    )
    harness = Harness(transport)
    status, body = harness(api_event(pull_request_payload()))
    assert (status, body) == (
        500,
        {
            "status": "error",
            "reason": "internal",
            "delivery_id": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
        },
    )
    assert len(posted_reviews(transport)) == 2
    assert "github.invalid_request" in harness.events()


def test_clean_pr_is_skipped_until_the_ai_review_exists():
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)])
    status, body = Harness(transport)(api_event(pull_request_payload()))
    assert (status, body["status"], body["reason"]) == (
        200,
        "skipped",
        "ai_not_enabled",
    )
    assert posted_reviews(transport) == []


def test_pr_without_patches_is_skipped_without_review_lookup():
    transport = github_api(files=[file_entry("logo.png", patch=None)])
    status, body = Harness(transport)(api_event(pull_request_payload()))
    assert (status, body["reason"]) == (200, "no_patches")
    assert transport.requests("GET", r"/reviews") == []


def test_binary_files_are_listed_in_the_security_review():
    transport = github_api(
        files=[
            file_entry("settings.py", LEAKY_PATCH),
            file_entry("logo.png", patch=None),
        ]
    )
    Harness(transport)(api_event(pull_request_payload()))
    assert (
        "Not reviewed (binary or too large): `logo.png`."
        in posted_reviews(transport)[0]["body"]
    )


def existing_review(login: str, commit_id: str = HEAD_SHA) -> dict:
    body = (
        MARKER_PREFIX + "kind=security sha=0123456 -->\n## 🚨 MergeGuard Security Alert"
    )
    return {"user": {"login": login}, "commit_id": commit_id, "body": body}


def test_redelivery_does_not_post_a_second_review():
    transport = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)],
        reviews=[existing_review(BOT_LOGIN)],
    )
    status, body = Harness(transport)(api_event(pull_request_payload()))
    assert (status, body["status"]) == (200, "duplicate")
    assert posted_reviews(transport) == []


@pytest.mark.parametrize(
    "review",
    [existing_review("attacker"), existing_review(BOT_LOGIN, commit_id="f" * 40)],
    ids=["copied-marker-by-another-user", "older-commit"],
)
def test_only_the_bots_review_of_this_commit_counts_as_duplicate(review):
    transport = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)], reviews=[review]
    )
    Harness(transport)(api_event(pull_request_payload()))
    assert len(posted_reviews(transport)) == 1


def test_dry_run_posts_nothing():
    transport = github_api(files=[file_entry("settings.py", LEAKY_PATCH)])
    harness = Harness(transport, env={"MERGEGUARD_DRY_RUN": "true"})
    status, body = harness(api_event(pull_request_payload()))
    assert (status, body["status"], body["reason"]) == (
        200,
        "dry_run",
        "REQUEST_CHANGES",
    )
    assert transport.requests("POST") == []
    [record] = [r for r in harness.records() if r["event"] == "review.dry_run"]
    assert record["rule_ids"] == ["aws-access-key-id"] and record["body_length"] > 0


# --- failures --------------------------------------------------------------------


def test_github_auth_failure_returns_500_and_logs_the_kind():
    transport = FakeTransport().route("GET", r"/files", json_response(401, {}))
    harness = Harness(transport)
    status, body = harness(api_event(pull_request_payload()))
    assert (status, body["status"]) == (500, "error")
    [record] = [r for r in harness.records() if r["event"] == "github.auth_failed"]
    assert record["http_status"] == 401


def test_missing_ssm_parameter_fails_closed_before_any_call():
    harness = Harness(FakeTransport(), ssm=FakeSsm({}))
    status, _ = harness(api_event(pull_request_payload()))
    assert status == 500
    assert harness.transport.calls == []
    assert "config.failed" in harness.events()


def test_invalid_settings_fail_closed():
    harness = Harness(FakeTransport(), env={"MERGEGUARD_MAX_FILES": "lots"})
    assert harness(api_event(pull_request_payload()))[0] == 500
    assert harness.ssm.calls == []


def test_unexpected_errors_return_500_with_the_type_only():
    transport = FakeTransport().route(
        "GET", r"/files", RuntimeError("boom: secret detail")
    )
    harness = Harness(transport)
    assert harness(api_event(pull_request_payload()))[0] == 500
    [record] = [r for r in harness.records() if r["event"] == "error.unhandled"]
    assert record["error_type"] == "RuntimeError"
    assert "secret detail" not in harness.logs.getvalue()


def test_retry_respects_the_lambda_deadline():
    class Context:
        def get_remaining_time_in_millis(self) -> int:
            return 3000

    transport = FakeTransport().route("GET", r"/files", TransportError("TimeoutError"))
    harness = Harness(transport)
    assert harness(api_event(pull_request_payload()), Context())[0] == 500
    assert len(transport.calls) == 1  # no time left for a retry


def test_time_budget_uses_the_lambda_deadline_when_available():
    class Context:
        def get_remaining_time_in_millis(self) -> int:
            return 15_000

    assert lambda_function._time_budget(Context())(11.0) is True
    assert lambda_function._time_budget(Context())(13.5) is False
    assert lambda_function._time_budget(None)(1_000.0) is True  # no context: allow


def test_post_failures_other_than_422_do_not_fall_back_to_a_comment():
    transport = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)],
        review_responses=(json_response(403, {}),),
    )
    harness = Harness(transport)
    assert harness(api_event(pull_request_payload()))[0] == 500
    assert [r["event"] for r in posted_reviews(transport)] == ["REQUEST_CHANGES"]
    assert "review.fallback_comment" not in harness.events()
    assert "github.forbidden" in harness.events()


# --- ST-06: log redaction ------------------------------------------------------


def test_st06_logs_never_contain_secrets_or_diff_content():
    leaky = file_entry("settings.py", LEAKY_PATCH)
    scenarios = [
        (github_api(files=[leaky]), {}),
        (github_api(files=[leaky], review_responses=(json_response(422, {}),)), {}),
        (github_api(files=[file_entry("a.py", CLEAN_PATCH)]), {}),
        (github_api(files=[leaky]), {"MERGEGUARD_DRY_RUN": "true"}),
        (FakeTransport().route("GET", r"/files", json_response(401, {})), {}),
    ]
    event = api_event(pull_request_payload())
    forbidden = [
        TEST_WEBHOOK_SECRET,
        TEST_GITHUB_TOKEN,
        AWS_EXAMPLE_KEY_ID,
        sign(event["body"].encode()),
        "AWS_KEY",
        "x = 2",
    ]
    for transport, env in scenarios:
        harness = Harness(transport, env=env)
        harness(event)
        harness(api_event(pull_request_payload(), signature="sha256=" + "0" * 64))
        output = harness.logs.getvalue()
        for value in forbidden:
            assert value not in output
        for record in harness.records():
            assert set(record) - {"ts", "level", "event"} <= ALLOWED_FIELDS


# --- Lambda entry point --------------------------------------------------------


def test_lambda_handler_wires_ssm_and_transport(monkeypatch):
    ssm = FakeSsm()
    monkeypatch.setattr(lambda_function, "_secret_store", None)
    monkeypatch.setattr(
        lambda_function, "SecretStore", lambda prefix: SecretStore(prefix, lambda: ssm)
    )
    monkeypatch.setattr(lambda_function, "urllib_transport", FakeTransport())
    monkeypatch.delenv("MERGEGUARD_PARAM_PREFIX", raising=False)
    event = api_event(load_fixture("webhooks/ping.json"), event="ping")
    assert lambda_function.lambda_handler(event, None)["statusCode"] == 200
    assert lambda_function.lambda_handler(event, None)["statusCode"] == 200
    assert len(ssm.calls) == 1  # the store is reused across invocations
