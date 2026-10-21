"""Phase 3 (M6), end to end with a fake GitHub, fake SSM, fake SQS and a mocked
Gemini endpoint. No AWS, GitHub or Gemini call is real.

Covers T-601 (GitHub App authentication), T-602 (inline comments) and T-603
(per-repository configuration); docs/TESTING_STRATEGY.md section 8.
"""

import json
from datetime import UTC, datetime

import pytest

import rsa_support
from mergeguard.models import PullRequestRef
from mergeguard.render import MARKER_PREFIX
from mergeguard.review_queue import build_message
from mergeguard.rsa_jwt import MIN_KEY_BITS, parse_private_key, verify_rs256
from support import (
    BOT_LOGIN,
    HEAD_SHA,
    QUEUE_URL,
    TEST_GEMINI_KEY,
    TEST_WEBHOOK_SECRET,
    FakeSqs,
    FakeSsm,
    FakeTransport,
    Harness,
    WorkerHarness,
    api_event,
    file_entry,
    github_api,
    json_response,
    pull_request_payload,
)

APP_ID = "123456"
APP_PEM = rsa_support.pkcs8_pem(MIN_KEY_BITS)
APP_KEY = parse_private_key(APP_PEM)
APP_BOT = "mergeguard-ai[bot]"
DELIVERY = "72d3162e-cc78-11e3-81ab-4c9367dc0958"
CLEAN_PATCH = "@@ -1 +1 @@\n-x = 1\n+x = 2"
OLD_SHA = "f" * 40
APP_ENV = {"MERGEGUARD_GITHUB_APP_ID": APP_ID}


def app_ssm(**overrides) -> FakeSsm:
    """SSM for App mode: the personal access token is deliberately absent."""
    values = {
        "/mergeguard/webhook_secret": TEST_WEBHOOK_SECRET,
        "/mergeguard/gemini_api_key": TEST_GEMINI_KEY,
        "/mergeguard/github_app_private_key": APP_PEM,
    }
    values.update(overrides)
    return FakeSsm(values)


def installation_token(value: str, seconds: int = 3600):
    expires = datetime.fromtimestamp(datetime.now(UTC).timestamp() + seconds, UTC)
    return json_response(
        201, {"token": value, "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}
    )


def app_api(*, tokens=("ghs_first",), slug="mergeguard-ai", **kwargs) -> FakeTransport:
    return (
        github_api(**kwargs)
        .route(
            "POST",
            r"/app/installations/\d+/access_tokens$",
            *[installation_token(value) for value in tokens],
        )
        .route("GET", r"api\.github\.com/app$", json_response(200, {"slug": slug}))
    )


def request_body(repo="owner/repo", installation_id: int | None = 42, sha=HEAD_SHA):
    pr = PullRequestRef(repo, 7, sha, installation_id=installation_id)
    return build_message(DELIVERY, "opened", pr, datetime(2026, 10, 21, tzinfo=UTC))


def run_worker(transport, *bodies, env=None, ssm=None):
    harness = WorkerHarness(
        transport, env={**APP_ENV, **(env or {})}, ssm=ssm or app_ssm()
    )
    return harness, harness(*bodies)


def posted_reviews(transport: FakeTransport) -> list[dict]:
    return [c.json() for c in transport.requests("POST", r"/pulls/\d+/reviews$")]


def token_calls(transport: FakeTransport) -> list:
    return transport.requests("POST", r"/access_tokens$")


def bot_review(
    review_id=11,
    *,
    login=APP_BOT,
    state="CHANGES_REQUESTED",
    commit_id=OLD_SHA,
    kind="security",
):
    return {
        "id": review_id,
        "user": {"login": login},
        "commit_id": commit_id,
        "state": state,
        "body": f"{MARKER_PREFIX}kind={kind} sha={commit_id[:7]} -->\n## review",
    }


# --- T-601: the App identity -------------------------------------------------


def test_the_worker_reviews_with_an_installation_token():
    transport = app_api(files=[file_entry("a.py", CLEAN_PATCH)])
    harness, result = run_worker(transport, request_body())
    assert result == {"batchItemFailures": []}
    assert len(posted_reviews(transport)) == 1
    # Every call to the repository carries the installation token, not the PAT.
    calls = [
        c for c in transport.calls if "/repos/" in c.url or c.url.endswith("/user")
    ]
    assert calls and all(
        c.headers["Authorization"] == "Bearer ghs_first" for c in calls
    )


def test_the_token_is_minted_for_one_repository_with_minimal_permissions():
    transport = app_api(files=[file_entry("a.py", CLEAN_PATCH)])
    run_worker(transport, request_body())
    [call] = token_calls(transport)
    assert call.url == "https://api.github.com/app/installations/42/access_tokens"
    assert call.json() == {
        "repositories": ["repo"],
        "permissions": {"pull_requests": "write"},
    }


def test_the_app_jwt_is_valid_and_never_used_for_repository_calls():
    transport = app_api(files=[file_entry("a.py", CLEAN_PATCH)])
    run_worker(transport, request_body())
    [mint] = token_calls(transport)
    header, claims, signature = mint.headers["Authorization"].split(" ")[1].split(".")
    padded = signature + "=" * (-len(signature) % 4)
    import base64

    assert verify_rs256(
        APP_KEY, f"{header}.{claims}".encode(), base64.urlsafe_b64decode(padded)
    )
    jwt = mint.headers["Authorization"]
    assert not any(
        c.headers["Authorization"] == jwt for c in transport.calls if "/repos/" in c.url
    )


def test_app_mode_never_reads_the_personal_access_token():
    transport = app_api(files=[file_entry("a.py", CLEAN_PATCH)])
    harness, _ = run_worker(transport, request_body())
    names = [name for call in harness.ssm.calls for name in call["Names"]]
    assert "/mergeguard/github_token" not in names
    assert sorted(names) == [
        "/mergeguard/gemini_api_key",
        "/mergeguard/github_app_private_key",
    ]
    assert transport.requests("GET", r"/user$") == []  # not allowed for App tokens


def test_reviews_are_matched_to_the_bot_account_not_the_token_owner():
    """TM-14 in App mode: the duplicate check uses <slug>[bot]."""
    own = bot_review(commit_id=HEAD_SHA, state="COMMENTED", kind="ai-review")
    transport = app_api(files=[file_entry("a.py", CLEAN_PATCH)], reviews=[own])
    _, result = run_worker(transport, request_body())
    assert result == {"batchItemFailures": []}
    assert posted_reviews(transport) == []  # duplicate

    spoof = bot_review(commit_id=HEAD_SHA, state="COMMENTED", login="someone-else")
    transport = app_api(files=[file_entry("a.py", CLEAN_PATCH)], reviews=[spoof])
    run_worker(transport, request_body())
    assert len(posted_reviews(transport)) == 1  # a copied marker does not count


def test_stale_blocks_by_the_bot_account_are_dismissed():
    transport = app_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        reviews=[bot_review(11), bot_review(12, login=BOT_LOGIN)],
    )
    run_worker(transport, request_body())
    dismissed = transport.requests("PUT", r"/dismissals$")
    assert [c.url.rsplit("/", 2)[-2] for c in dismissed] == ["11"]


def test_each_repository_gets_its_own_token_and_tokens_are_reused():
    transport = app_api(
        tokens=("ghs_one", "ghs_two"), files=[file_entry("a.py", CLEAN_PATCH)]
    )
    harness, result = run_worker(
        transport,
        request_body("owner/repo", 42),
        request_body("other/lib", 99),
        request_body("owner/repo", 42),
    )
    assert result == {"batchItemFailures": []}
    scopes = [
        (c.url.split("/")[5], c.json()["repositories"]) for c in token_calls(transport)
    ]
    assert scopes == [("42", ["repo"]), ("99", ["lib"])]  # the third reused the first
    reviews = transport.requests("POST", r"/pulls/\d+/reviews$")
    assert [c.headers["Authorization"] for c in reviews] == [
        "Bearer ghs_one",
        "Bearer ghs_two",
        "Bearer ghs_one",
    ]


def test_the_commit_status_and_config_permissions_are_requested_only_when_needed():
    transport = app_api(files=[file_entry("a.py", CLEAN_PATCH)])
    run_worker(
        transport,
        request_body(),
        env={"MERGEGUARD_COMMIT_STATUS": "true", "MERGEGUARD_REPO_CONFIG": "true"},
    )
    assert token_calls(transport)[0].json()["permissions"] == {
        "pull_requests": "write",
        "contents": "read",
        "statuses": "write",
    }


def test_a_message_without_an_installation_fails_closed_in_app_mode():
    transport = app_api(files=[file_entry("a.py", CLEAN_PATCH)])
    harness, result = run_worker(transport, request_body(installation_id=None))
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert "config.failed" in harness.events()
    assert transport.calls == []


@pytest.mark.parametrize(
    ("status", "event"),
    [
        (401, "github.auth_failed"),
        (404, "github.forbidden"),
        (500, "github.unavailable"),
    ],
)
def test_token_errors_are_retried_through_the_queue(status, event):
    transport = app_api(files=[file_entry("a.py", CLEAN_PATCH)])
    transport._routes.insert(
        0,
        (
            "POST",
            __import__("re").compile(r"/access_tokens$"),
            [json_response(status, {})],
        ),
    )
    harness, result = run_worker(transport, request_body())
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert event in harness.events()
    assert posted_reviews(transport) == []


def test_an_unusable_or_placeholder_private_key_fails_closed():
    for value in ("not a key", "mergeguard-placeholder:set-with-aws-ssm-put-parameter"):
        transport = app_api(files=[file_entry("a.py", CLEAN_PATCH)])
        ssm = app_ssm(**{"/mergeguard/github_app_private_key": value})
        harness, result = run_worker(transport, request_body(), ssm=ssm)
        assert result == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
        assert "config.failed" in harness.events()
        assert value not in harness.logs.getvalue()
        assert transport.calls == []


def test_the_secret_gate_still_blocks_in_app_mode():
    from support import AWS_EXAMPLE_KEY_ID

    patch = f"@@ -0,0 +1 @@\n+k = '{AWS_EXAMPLE_KEY_ID}'"
    transport = app_api(files=[file_entry("s.py", patch)])
    run_worker(transport, request_body())
    assert posted_reviews(transport)[0]["event"] == "REQUEST_CHANGES"
    assert transport.requests("POST", r"generativelanguage") == []


# --- the webhook side ---------------------------------------------------------


def test_the_receiver_queues_the_installation_id():
    sqs = FakeSqs()
    payload = pull_request_payload()
    payload["installation"] = {"id": 42}
    harness = Harness(FakeTransport(), env={"MERGEGUARD_QUEUE_URL": QUEUE_URL}, sqs=sqs)
    assert harness(api_event(payload))[0] == 202
    assert json.loads(sqs.sent[0]["MessageBody"])["installation_id"] == 42


def test_a_repository_webhook_queues_no_installation_id():
    sqs = FakeSqs()
    harness = Harness(FakeTransport(), env={"MERGEGUARD_QUEUE_URL": QUEUE_URL}, sqs=sqs)
    harness(api_event(pull_request_payload()))
    assert "installation_id" not in json.loads(sqs.sent[0]["MessageBody"])


@pytest.mark.parametrize("installation", [{"id": 0}, {"id": "42"}, {}, [], 7])
def test_a_malformed_installation_is_rejected(installation):
    payload = pull_request_payload()
    payload["installation"] = installation
    status, body = Harness(FakeTransport())(api_event(payload))
    assert (status, body["reason"]) == (400, "malformed_payload")


def test_the_receiver_in_app_mode_reads_only_the_webhook_secret():
    sqs = FakeSqs()
    ssm = FakeSsm({"/mergeguard/webhook_secret": TEST_WEBHOOK_SECRET})
    payload = pull_request_payload()
    payload["installation"] = {"id": 42}
    harness = Harness(
        FakeTransport(),
        env={"MERGEGUARD_QUEUE_URL": QUEUE_URL, **APP_ENV},
        ssm=ssm,
        sqs=sqs,
    )
    assert harness(api_event(payload))[0] == 202
    assert [c["Names"] for c in ssm.calls] == [["/mergeguard/webhook_secret"]]


def test_inline_mode_in_app_mode_reads_no_token_either():
    transport = app_api(files=[file_entry("a.py", CLEAN_PATCH)])
    ssm = app_ssm()
    payload = pull_request_payload()
    payload["installation"] = {"id": 42}
    harness = Harness(transport, env=APP_ENV, ssm=ssm)
    status, body = harness(api_event(payload))
    assert (status, body["status"]) == (200, "reviewed")
    names = [n for c in ssm.calls for n in c["Names"]]
    assert "/mergeguard/github_token" not in names
    assert len(posted_reviews(transport)) == 1
