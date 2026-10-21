"""Phase 2 (M5), end to end with a fake GitHub, fake SSM, fake SQS and a mocked
Gemini endpoint. No AWS or Gemini call is real.

Covers T-501 (receiver and worker), T-502 (commit status), T-503 (stale review
dismissal) and T-504 (suppressions); docs/TESTING_STRATEGY.md section 8.
"""

import json
from datetime import UTC, datetime

import pytest

import lambda_function
from mergeguard.http import TransportError
from mergeguard.logging_util import ALLOWED_FIELDS
from mergeguard.models import PullRequestRef
from mergeguard.render import FALLBACK_CALLOUT, MARKER_PREFIX
from mergeguard.review_queue import build_message
from support import (
    AWS_EXAMPLE_KEY_ID,
    BOT_LOGIN,
    GEMINI_ROUTE,
    HEAD_SHA,
    QUEUE_URL,
    TEST_GEMINI_KEY,
    TEST_GITHUB_TOKEN,
    TEST_WEBHOOK_SECRET,
    FakeSqs,
    FakeSsm,
    FakeTransport,
    Harness,
    WorkerHarness,
    api_event,
    file_entry,
    gemini_response,
    github_api,
    json_response,
    pull_request_payload,
    pull_state,
    review_json,
    sign,
)

LEAKY_PATCH = f"@@ -0,0 +1,2 @@\n+import os\n+AWS_KEY = '{AWS_EXAMPLE_KEY_ID}'"
CLEAN_PATCH = "@@ -1 +1 @@\n-x = 1\n+x = 2"
MARKER = "mergeguard:ignore-secret"
OLD_SHA = "f" * 40
DELIVERY = "72d3162e-cc78-11e3-81ab-4c9367dc0958"
STATUS_ENV = {"MERGEGUARD_COMMIT_STATUS": "true"}
RECEIVER_ENV = {"MERGEGUARD_QUEUE_URL": QUEUE_URL}


def pr_ref() -> PullRequestRef:
    return PullRequestRef("owner/repo", 7, HEAD_SHA)


def request_body(action: str = "opened", sha: str = HEAD_SHA) -> str:
    pr = PullRequestRef("owner/repo", 7, sha)
    return build_message(DELIVERY, action, pr, datetime(2026, 10, 21, tzinfo=UTC))


def posted_reviews(transport: FakeTransport) -> list[dict]:
    return [c.json() for c in transport.requests("POST", r"/pulls/7/reviews$")]


def statuses(transport: FakeTransport) -> list[dict]:
    return [c.json() for c in transport.requests("POST", r"/statuses/")]


def dismissals(transport: FakeTransport) -> list:
    return transport.requests("PUT", r"/dismissals$")


def gemini_calls(transport: FakeTransport) -> list:
    return transport.requests("POST", GEMINI_ROUTE)


def bot_review(
    review_id: int = 11,
    *,
    state: str = "CHANGES_REQUESTED",
    commit_id: str = OLD_SHA,
    kind: str = "security",
    login: str = BOT_LOGIN,
    body: str | None = None,
) -> dict:
    text = (
        body
        if body is not None
        else f"{MARKER_PREFIX}kind={kind} sha={commit_id[:7]} -->\n## review"
    )
    return {
        "id": review_id,
        "user": {"login": login},
        "commit_id": commit_id,
        "state": state,
        "body": text,
    }


def run_worker(transport, *bodies, env=None, ssm=None, context=None):
    harness = WorkerHarness(transport, env=env, ssm=ssm)
    return harness, harness(*bodies, context=context)


# --- T-501: receiver ---------------------------------------------------------


def test_receiver_acknowledges_with_202_and_queues_metadata_only():
    sqs = FakeSqs()
    harness = Harness(FakeTransport(), env=RECEIVER_ENV, sqs=sqs)
    status, body = harness(api_event(pull_request_payload(action="synchronize")))
    assert (status, body["status"]) == (202, "accepted")
    assert "reason" not in body and body["delivery_id"] == DELIVERY
    [sent] = sqs.sent
    assert sent["QueueUrl"] == QUEUE_URL
    message = json.loads(sent["MessageBody"])
    assert message["schema"] == "mergeguard.review-request.v1"
    assert (message["repository"], message["pull_number"]) == ("owner/repo", 7)
    assert (message["head_sha"], message["action"]) == (HEAD_SHA, "synchronize")
    assert message["delivery_id"] == DELIVERY
    assert "octocat" not in sent["MessageBody"]  # no author, title or code
    assert "queue.enqueued" in harness.events()


def test_receiver_makes_no_github_or_gemini_call():
    harness = Harness(FakeTransport(), env=RECEIVER_ENV, sqs=FakeSqs())
    harness(api_event(pull_request_payload()))
    assert harness.transport.calls == []  # the worker does all of that


def test_receiver_reads_only_the_webhook_secret():
    """Least privilege: it works even when the token and Gemini key are not
    readable by its role."""
    ssm = FakeSsm({"/mergeguard/webhook_secret": TEST_WEBHOOK_SECRET})
    harness = Harness(FakeTransport(), env=RECEIVER_ENV, ssm=ssm, sqs=FakeSqs())
    assert harness(api_event(pull_request_payload()))[0] == 202
    assert [call["Names"] for call in ssm.calls] == [["/mergeguard/webhook_secret"]]


def test_receiver_verifies_the_signature_before_queueing():
    sqs = FakeSqs()
    harness = Harness(FakeTransport(), env=RECEIVER_ENV, sqs=sqs)
    event = api_event(pull_request_payload())
    event["body"] = event["body"].replace("opened", "closed")
    status, body = harness(event)
    assert (status, body["reason"]) == (401, "invalid_signature")
    assert sqs.sent == []


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"payload": pull_request_payload(action="closed")}, "action_not_supported"),
        ({"payload": pull_request_payload(pull_request__draft=True)}, "draft"),
        ({"payload": {"zen": "x"}, "event": "issues"}, "event_not_supported"),
    ],
)
def test_receiver_queues_nothing_for_ignored_events(kwargs, expected):
    sqs = FakeSqs()
    harness = Harness(FakeTransport(), env=RECEIVER_ENV, sqs=sqs)
    payload = kwargs.pop("payload")
    status, body = harness(api_event(payload, **kwargs))
    assert (status, body["status"], body["reason"]) == (200, "ignored", expected)
    assert sqs.sent == []


def test_receiver_answers_ping_without_queueing():
    sqs = FakeSqs()
    harness = Harness(FakeTransport(), env=RECEIVER_ENV, sqs=sqs)
    status, body = harness(
        api_event({"zen": "Keep it logically awesome."}, event="ping")
    )
    assert (status, body["status"]) == (200, "pong")
    assert sqs.sent == []


def test_a_failed_enqueue_is_a_500_so_the_delivery_can_be_redelivered():
    sqs = FakeSqs(error=RuntimeError("AccessDenied arn:aws:sqs:us-east-1:1:q"))
    harness = Harness(FakeTransport(), env=RECEIVER_ENV, sqs=sqs)
    status, body = harness(api_event(pull_request_payload()))
    assert (status, body["status"], body["reason"]) == (500, "error", "internal")
    assert "queue.failed" in harness.events()
    assert "AccessDenied" not in harness.logs.getvalue()


def test_queue_url_without_a_wired_queue_fails_closed():
    harness = Harness(FakeTransport(), env=RECEIVER_ENV)  # no sqs
    assert harness(api_event(pull_request_payload()))[0] == 500
    assert "config.failed" in harness.events()


def test_invalid_queue_url_fails_closed():
    env = {"MERGEGUARD_QUEUE_URL": "https://evil.example.com/123456789012/q"}
    harness = Harness(FakeTransport(), env=env, sqs=FakeSqs())
    assert harness(api_event(pull_request_payload()))[0] == 500
    assert harness.ssm.calls == []


def test_without_a_queue_url_the_review_still_runs_inline():
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)])
    status, body = Harness(transport, sqs=FakeSqs())(api_event(pull_request_payload()))
    assert (status, body["status"]) == (200, "reviewed")


def test_lambda_handler_caches_the_queue_between_invocations(monkeypatch):
    sqs = FakeSqs()
    monkeypatch.setattr(lambda_function, "_secret_store", None)
    monkeypatch.setattr(lambda_function, "_review_queue", None)
    monkeypatch.setattr(lambda_function, "urllib_transport", FakeTransport())
    ssm = FakeSsm()
    monkeypatch.setattr(
        lambda_function,
        "SecretStore",
        lambda prefix: __import__(
            "mergeguard.config", fromlist=["SecretStore"]
        ).SecretStore(prefix, lambda: ssm),
    )
    monkeypatch.setattr(
        lambda_function,
        "ReviewQueue",
        lambda url: __import__(
            "mergeguard.review_queue", fromlist=["ReviewQueue"]
        ).ReviewQueue(url, lambda: sqs),
    )
    monkeypatch.setenv("MERGEGUARD_QUEUE_URL", QUEUE_URL)
    event = api_event(pull_request_payload())
    first = lambda_function.lambda_handler(event, None)
    queue = lambda_function._review_queue
    second = lambda_function.lambda_handler(event, None)
    assert (first["statusCode"], second["statusCode"]) == (202, 202)
    assert lambda_function._review_queue is queue and len(sqs.sent) == 2


# --- T-501: worker -----------------------------------------------------------


def test_worker_reviews_the_queued_pull_request():
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)])
    harness, result = run_worker(transport, request_body())
    assert result == {"batchItemFailures": []}
    [review] = posted_reviews(transport)
    assert review["commit_id"] == HEAD_SHA and review["event"] == "COMMENT"
    assert harness.events() == [
        "worker.received",
        "files.fetched",
        "scan.completed",
        "ai.completed",
        "review.posted",
        "worker.completed",
    ]
    done = harness.records()[-1]
    assert (done["status"], done["reason"], done["delivery_id"]) == (
        "reviewed",
        "COMMENT",
        DELIVERY,
    )


def test_worker_reads_the_token_and_gemini_key_but_not_the_webhook_secret():
    harness, _ = run_worker(
        github_api(files=[file_entry("a.py", CLEAN_PATCH)]), request_body()
    )
    names = [name for call in harness.ssm.calls for name in call["Names"]]
    assert sorted(names) == ["/mergeguard/gemini_api_key", "/mergeguard/github_token"]


def test_worker_with_a_secret_finding_never_calls_gemini():
    transport = github_api(files=[file_entry("settings.py", LEAKY_PATCH)])
    _, result = run_worker(transport, request_body())
    assert result == {"batchItemFailures": []}
    assert posted_reviews(transport)[0]["event"] == "REQUEST_CHANGES"
    assert gemini_calls(transport) == []


def test_worker_reports_only_failed_records_in_a_batch():
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)])
    harness, result = run_worker(transport, request_body(), "not a message")
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}
    assert len(posted_reviews(transport)) == 1
    [rejected] = [r for r in harness.records() if r["event"] == "worker.rejected"]
    assert rejected["reason"] == "malformed_message"


def test_worker_with_nothing_to_do_returns_no_failures():
    assert WorkerHarness(FakeTransport())() == {"batchItemFailures": []}
    assert worker_handle({}) == {"batchItemFailures": []}


def worker_handle(event: dict) -> dict:
    import worker_function

    store = __import__("mergeguard.config", fromlist=["SecretStore"]).SecretStore(
        "/mergeguard", FakeSsm
    )
    return worker_function.handle(
        event,
        None,
        environ={},
        secrets=lambda prefix: store,
        transport=FakeTransport(),
    )


@pytest.mark.parametrize(
    ("transport", "ssm", "event_name"),
    [
        (
            FakeTransport()
            .route("GET", r"/pulls/7$", pull_state())
            .route("GET", r"/files", json_response(401, {})),
            None,
            "github.auth_failed",
        ),
        (FakeTransport(), FakeSsm({}), "config.failed"),
        (
            FakeTransport().route("GET", r"/pulls/7$", RuntimeError("boom: detail")),
            None,
            "error.unhandled",
        ),
    ],
    ids=["github-auth", "ssm-missing", "unexpected"],
)
def test_worker_failures_are_retried_through_the_queue(transport, ssm, event_name):
    harness, result = run_worker(transport, request_body(), ssm=ssm)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert event_name in harness.events()
    assert "detail" not in harness.logs.getvalue()


def test_worker_with_invalid_settings_fails_every_record():
    harness, result = run_worker(
        FakeTransport(),
        request_body(),
        request_body(),
        env={"MERGEGUARD_MAX_FILES": "lots"},
    )
    assert result == {
        "batchItemFailures": [{"itemIdentifier": "m-0"}, {"itemIdentifier": "m-1"}]
    }
    assert harness.events() == ["config.failed"]
    assert harness.ssm.calls == []


def test_worker_skips_when_the_head_has_moved_on():
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)], pull=(pull_state(OLD_SHA),)
    )
    harness, result = run_worker(transport, request_body())
    assert result == {"batchItemFailures": []}  # done: the newer SHA has its own run
    assert posted_reviews(transport) == [] and gemini_calls(transport) == []
    assert transport.requests("GET", r"/files") == []  # not even fetched
    skipped = [r for r in harness.records() if r["event"] == "review.skipped"]
    assert skipped[0]["reason"] == "stale_head"


def test_worker_skips_a_closed_pull_request():
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)], pull=(pull_state(state="closed"),)
    )
    harness, result = run_worker(transport, request_body())
    assert result == {"batchItemFailures": []}
    assert posted_reviews(transport) == []
    assert any(
        r.get("reason") == "pr_closed"
        for r in harness.records()
        if r["event"] == "review.skipped"
    )


def test_worker_does_not_post_when_the_head_moves_during_the_ai_call():
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        pull=(pull_state(), pull_state(OLD_SHA)),  # current at first, then moved
    )
    harness, result = run_worker(transport, request_body(), env=STATUS_ENV)
    assert result == {"batchItemFailures": []}
    assert len(gemini_calls(transport)) == 1
    assert posted_reviews(transport) == []
    assert "stale_head" in harness.logs.getvalue()


def test_worker_absorbs_a_duplicate_message():
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        reviews=[bot_review(state="COMMENTED", commit_id=HEAD_SHA, kind="ai-review")],
    )
    harness, result = run_worker(transport, request_body())
    assert result == {"batchItemFailures": []}
    assert posted_reviews(transport) == [] and gemini_calls(transport) == []
    assert "review.duplicate" in harness.events()


def test_worker_dry_run_writes_nothing():
    transport = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)],
        reviews=[bot_review()],
    )
    env = {"MERGEGUARD_DRY_RUN": "true", **STATUS_ENV}
    _, result = run_worker(transport, request_body(), env=env)
    assert result == {"batchItemFailures": []}
    assert transport.requests("POST") == [] and transport.requests("PUT") == []


def test_receiver_message_flows_through_the_worker():
    sqs = FakeSqs()
    receiver = Harness(FakeTransport(), env=RECEIVER_ENV, sqs=sqs)
    receiver(api_event(pull_request_payload()))
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)])
    _, result = run_worker(transport, sqs.sent[0]["MessageBody"])
    assert result == {"batchItemFailures": []}
    assert posted_reviews(transport)[0]["commit_id"] == HEAD_SHA


def test_a_retry_after_a_crash_finishes_the_job_without_a_second_review():
    """First attempt: the review is posted, then the status call fails. The
    retry finds the review (duplicate) and still sets the final status."""
    first = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)],
        status_responses=(json_response(201, {}), json_response(502, {})),
    )
    _, failed = run_worker(first, request_body(), env=STATUS_ENV)
    assert failed == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert len(posted_reviews(first)) == 1
    assert [s["state"] for s in statuses(first)][:2] == ["pending", "failure"]

    posted = posted_reviews(first)[0]
    existing = bot_review(
        state="CHANGES_REQUESTED", commit_id=HEAD_SHA, body=posted["body"]
    )
    retry = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)], reviews=[existing]
    )
    _, done = run_worker(retry, request_body(), env=STATUS_ENV)
    assert done == {"batchItemFailures": []}
    assert posted_reviews(retry) == []
    assert [s["state"] for s in statuses(retry)] == ["failure"]


# --- T-502: commit status ----------------------------------------------------


def test_commit_status_is_off_unless_enabled():
    transport = github_api(files=[file_entry("settings.py", LEAKY_PATCH)])
    run_worker(transport, request_body())
    assert statuses(transport) == []


def test_secret_findings_set_pending_then_failure_on_the_head_sha():
    transport = github_api(files=[file_entry("settings.py", LEAKY_PATCH)])
    harness, _ = run_worker(transport, request_body(), env=STATUS_ENV)
    assert statuses(transport) == [
        {
            "state": "pending",
            "context": "mergeguard/review",
            "description": "MergeGuard review in progress",
        },
        {
            "state": "failure",
            "context": "mergeguard/review",
            "description": "Possible credentials found — see review",
        },
    ]
    urls = [c.url for c in transport.requests("POST", r"/statuses/")]
    assert all(url.endswith(f"/statuses/{HEAD_SHA}") for url in urls)
    assert [
        r["commit_state"] for r in harness.records() if r["event"] == "status.set"
    ] == [
        "pending",
        "failure",
    ]


def test_pending_is_set_before_the_scan_and_the_final_status_after_the_review():
    transport = github_api(files=[file_entry("settings.py", LEAKY_PATCH)])
    run_worker(transport, request_body(), env=STATUS_ENV)
    order = [
        (
            "status"
            if "/statuses/" in c.url
            else "review"
            if c.url.endswith("/reviews")
            else "x"
        )
        for c in transport.calls
        if c.method == "POST"
    ]
    assert order == ["status", "review", "status"]


@pytest.mark.parametrize(
    ("gemini", "state", "description"),
    [
        (
            gemini_response(review_json("LOW", 0)),
            "success",
            "No blocking issues found",
        ),
        (
            gemini_response(review_json("MEDIUM", 0)),
            "failure",
            "Changes requested by AI review — see review",
        ),
        (
            gemini_response(review_json("LOW", 2)),
            "failure",
            "Changes requested by AI review — see review",
        ),
        (TransportError("timeout"), "success", "No blocking issues found"),
    ],
    ids=["low", "medium", "bugs", "ai-unavailable"],
)
def test_ai_outcomes_map_to_the_documented_status(gemini, state, description):
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)], gemini=(gemini,))
    run_worker(transport, request_body(), env=STATUS_ENV)
    assert [s["state"] for s in statuses(transport)] == ["pending", state]
    assert statuses(transport)[-1]["description"] == description


def test_the_own_pr_fallback_still_reports_failure():
    transport = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)],
        review_responses=(json_response(422, {}), json_response(200, {})),
    )
    run_worker(transport, request_body(), env=STATUS_ENV)
    assert [r["event"] for r in posted_reviews(transport)] == [
        "REQUEST_CHANGES",
        "COMMENT",
    ]
    assert [s["state"] for s in statuses(transport)] == ["pending", "failure"]


def test_a_pr_without_patches_gets_success_and_no_pending():
    transport = github_api(files=[file_entry("logo.png", patch=None)])
    run_worker(transport, request_body(), env=STATUS_ENV)
    assert [s["state"] for s in statuses(transport)] == ["success"]


def test_an_internal_failure_sets_the_error_status():
    transport = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)],
        review_responses=(json_response(403, {}),),
    )
    _, result = run_worker(transport, request_body(), env=STATUS_ENV)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert [s["state"] for s in statuses(transport)] == ["pending", "error"]
    assert statuses(transport)[-1]["description"] == (
        "MergeGuard could not complete the review"
    )


def test_a_failing_error_status_does_not_mask_the_original_failure():
    transport = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)],
        review_responses=(json_response(403, {}),),
        status_responses=(json_response(201, {}), json_response(502, {})),
    )
    harness, result = run_worker(transport, request_body(), env=STATUS_ENV)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert "github.forbidden" in harness.events()


def test_a_missing_status_permission_never_blocks_the_review():
    transport = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)],
        status_responses=(json_response(403, {}),),
    )
    harness, result = run_worker(transport, request_body(), env=STATUS_ENV)
    assert result == {"batchItemFailures": []}
    assert posted_reviews(transport)[0]["event"] == "REQUEST_CHANGES"
    failed = [r for r in harness.records() if r["event"] == "status.failed"]
    assert [(r["reason"], r["http_status"], r["level"]) for r in failed] == [
        ("forbidden", 403, "WARNING"),
        ("forbidden", 403, "WARNING"),
    ]


def test_a_transient_status_failure_is_retried():
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        status_responses=(json_response(502, {}),),
    )
    _, result = run_worker(transport, request_body(), env=STATUS_ENV)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert posted_reviews(transport) == []  # failed before any review work


def test_inline_mode_also_sets_statuses_when_enabled():
    transport = github_api(files=[file_entry("settings.py", LEAKY_PATCH)])
    status, body = Harness(transport, env=STATUS_ENV)(api_event(pull_request_payload()))
    assert (status, body["reason"]) == (200, "REQUEST_CHANGES")
    assert [s["state"] for s in statuses(transport)] == ["pending", "failure"]
    assert transport.requests("GET", r"/pulls/7$") == []  # no head re-check inline


@pytest.mark.parametrize(
    ("existing", "state"),
    [
        (bot_review(commit_id=HEAD_SHA, kind="security"), "failure"),
        (bot_review(commit_id=HEAD_SHA, kind="ai-review"), "failure"),
        (
            bot_review(
                commit_id=HEAD_SHA,
                state="COMMENTED",
                kind="ai-review",
                body=f"{MARKER_PREFIX}kind=ai-review sha=0123456 -->\n{FALLBACK_CALLOUT}",
            ),
            "failure",
        ),
        (
            bot_review(
                commit_id=HEAD_SHA,
                state="COMMENTED",
                kind="security",
                body=f"{MARKER_PREFIX}kind=security sha=0123456 -->\n{FALLBACK_CALLOUT}",
            ),
            "failure",
        ),
        (
            bot_review(commit_id=HEAD_SHA, state="COMMENTED", kind="ai-review"),
            "success",
        ),
        (
            bot_review(commit_id=HEAD_SHA, state="COMMENTED", kind="ai-unavailable"),
            "success",
        ),
    ],
    ids=["security", "ai-blocking", "ai-fallback", "security-fallback", "low", "aiu"],
)
def test_a_duplicate_restores_the_status_from_the_existing_review(existing, state):
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)], reviews=[existing])
    run_worker(transport, request_body(), env=STATUS_ENV)
    assert [s["state"] for s in statuses(transport)] == [state]  # never `pending`
    assert posted_reviews(transport) == []


# --- T-503: stale review dismissal ------------------------------------------


def test_a_fixed_pr_dismisses_the_earlier_blocking_review():
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        reviews=[bot_review(11), bot_review(12, kind="ai-review")],
    )
    harness, _ = run_worker(transport, request_body())
    assert posted_reviews(transport)[0]["event"] == "COMMENT"
    calls = dismissals(transport)
    assert [c.url.rsplit("/", 2)[-2] for c in calls] == ["11", "12"]
    assert calls[0].json()["event"] == "DISMISS"
    assert "commit 0123456" in calls[0].json()["message"]
    # The new review is posted first, so a failed dismissal never loses it.
    methods = [c.method for c in transport.calls if "/reviews" in c.url][1:]
    assert methods.index("POST") < methods.index("PUT")
    [done] = [r for r in harness.records() if r["event"] == "review.dismissed"]
    assert done["dismissed"] == 2


def test_an_ai_unavailable_comment_keeps_an_earlier_ai_block():
    """A failed review must not clear a real AI-based block (FR-018)."""
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        reviews=[bot_review(11, kind="ai-review")],
        gemini=(TransportError("timeout"),),
    )
    run_worker(transport, request_body())
    assert posted_reviews(transport)[0]["event"] == "COMMENT"
    assert dismissals(transport) == []


def test_an_ai_unavailable_comment_still_ends_an_earlier_secret_block():
    """The new commit's deterministic scan was clean, so the secret block is
    resolved even though the AI review failed."""
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        reviews=[bot_review(11, kind="security"), bot_review(12, kind="ai-review")],
        gemini=(TransportError("timeout"),),
    )
    run_worker(transport, request_body())
    assert [c.url.rsplit("/", 2)[-2] for c in dismissals(transport)] == ["11"]


def test_a_retry_after_an_ai_failure_keeps_the_ai_block_too():
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        reviews=[
            bot_review(11, kind="ai-review"),
            bot_review(
                13, state="COMMENTED", commit_id=HEAD_SHA, kind="ai-unavailable"
            ),
        ],
    )
    run_worker(transport, request_body())
    assert posted_reviews(transport) == [] and dismissals(transport) == []


@pytest.mark.parametrize(
    "review",
    [
        bot_review(11, login="attacker"),  # a copied marker from someone else
        bot_review(11, body="## no marker here"),
        bot_review(11, state="DISMISSED"),
        bot_review(11, state="COMMENTED"),
        bot_review(11, commit_id=HEAD_SHA),  # a review of the current head
        {**bot_review(11), "id": "11"},
        {**bot_review(11), "id": None},
    ],
    ids=[
        "other-author",
        "no-marker",
        "already-dismissed",
        "comment-only",
        "current-head",
        "string-id",
        "no-id",
    ],
)
def test_only_the_bots_older_blocking_reviews_are_dismissed(review):
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)], reviews=[review])
    run_worker(transport, request_body())
    assert dismissals(transport) == []


def test_a_blocking_result_supersedes_by_itself_and_dismisses_nothing():
    transport = github_api(
        files=[file_entry("settings.py", LEAKY_PATCH)], reviews=[bot_review(11)]
    )
    run_worker(transport, request_body())
    assert dismissals(transport) == []


def test_a_pr_without_patches_keeps_the_documented_scope():
    """FR-018 covers a newer head that ends in COMMENT; a patch-less PR posts no
    review and makes no review lookup."""
    transport = github_api(
        files=[file_entry("logo.png", patch=None)], reviews=[bot_review(11)]
    )
    run_worker(transport, request_body(), env=STATUS_ENV)
    assert dismissals(transport) == []


def test_a_failed_dismissal_is_logged_and_stops_after_the_first():
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        reviews=[bot_review(11), bot_review(12)],
        dismissal_responses=(json_response(403, {}),),
    )
    harness, result = run_worker(transport, request_body())
    assert result == {"batchItemFailures": []}  # the review itself was posted
    assert len(posted_reviews(transport)) == 1
    assert len(dismissals(transport)) == 1
    [failed] = [r for r in harness.records() if r["event"] == "review.dismissal_failed"]
    assert (failed["reason"], failed["http_status"]) == ("forbidden", 403)
    assert "review.dismissed" not in harness.events()


def test_a_transient_dismissal_failure_is_retried_and_the_retry_finishes_it():
    first = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        reviews=[bot_review(11)],
        dismissal_responses=(json_response(502, {}),),
    )
    _, failed = run_worker(first, request_body())
    assert failed == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    posted = posted_reviews(first)[0]
    # On the retry the new review exists, so the duplicate path dismisses.
    retry = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        reviews=[
            bot_review(11),
            bot_review(13, state="COMMENTED", commit_id=HEAD_SHA, body=posted["body"]),
        ],
    )
    _, done = run_worker(retry, request_body())
    assert done == {"batchItemFailures": []}
    assert posted_reviews(retry) == [] and len(dismissals(retry)) == 1


def test_dismissal_is_skipped_in_dry_run():
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)], reviews=[bot_review(11)]
    )
    run_worker(transport, request_body(), env={"MERGEGUARD_DRY_RUN": "true"})
    assert dismissals(transport) == []


def test_inline_mode_dismisses_too():
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)], reviews=[bot_review(11)]
    )
    Harness(transport)(api_event(pull_request_payload()))
    assert len(dismissals(transport)) == 1


# --- T-504: suppressions -----------------------------------------------------


def suppressed_patch(marker: bool = True) -> str:
    suffix = f"  # {MARKER}" if marker else ""
    return f"@@ -0,0 +1,2 @@\n+import os\n+AWS_KEY = '{AWS_EXAMPLE_KEY_ID}'{suffix}"


def test_inline_marker_lets_the_pr_through_and_lists_the_suppression():
    transport = github_api(files=[file_entry("docs.py", suppressed_patch())])
    harness, result = run_worker(transport, request_body())
    assert result == {"batchItemFailures": []}
    [review] = posted_reviews(transport)
    assert review["event"] == "COMMENT"
    assert "1 finding(s) suppressed by `mergeguard:ignore-secret`" in review["body"]
    assert "- AWS access key ID: `docs.py` line 2 (inline marker)" in review["body"]
    assert AWS_EXAMPLE_KEY_ID not in review["body"]
    [scan] = [r for r in harness.records() if r["event"] == "scan.completed"]
    assert (scan["suppressed"], scan["rule_ids"]) == (1, [])


def test_the_same_line_without_the_marker_is_blocked():
    transport = github_api(files=[file_entry("docs.py", suppressed_patch(False))])
    run_worker(transport, request_body())
    assert posted_reviews(transport)[0]["event"] == "REQUEST_CHANGES"
    assert gemini_calls(transport) == []


def test_suppressed_lines_are_withheld_from_the_ai():
    transport = github_api(
        files=[file_entry("docs.py", suppressed_patch())],
        gemini=(gemini_response(review_json("LOW", 0)),),
    )
    run_worker(transport, request_body())
    [call] = gemini_calls(transport)
    sent = call.body.decode()
    assert AWS_EXAMPLE_KEY_ID not in sent and MARKER not in sent
    assert "[line withheld: suppressed secret finding]" in sent
    assert "import os" in sent  # the rest of the diff still gets reviewed


def test_the_path_allow_list_suppresses_by_file():
    transport = github_api(
        files=[file_entry("tests/fixtures/keys.py", suppressed_patch(False))]
    )
    env = {"MERGEGUARD_SCAN_ALLOW_PATHS": "tests/fixtures/**"}
    _, result = run_worker(transport, request_body(), env=env)
    assert result == {"batchItemFailures": []}
    [review] = posted_reviews(transport)
    assert review["event"] == "COMMENT"
    assert (
        "- AWS access key ID: `tests/fixtures/keys.py` line 2 (path allow-list)"
        in review["body"]
    )
    assert AWS_EXAMPLE_KEY_ID not in gemini_calls(transport)[0].body.decode()


def test_the_allow_list_does_not_cover_other_paths():
    transport = github_api(files=[file_entry("src/app.py", suppressed_patch(False))])
    env = {"MERGEGUARD_SCAN_ALLOW_PATHS": "tests/fixtures/**"}
    run_worker(transport, request_body(), env=env)
    assert posted_reviews(transport)[0]["event"] == "REQUEST_CHANGES"


def test_unsuppressed_findings_still_block_and_the_suppressed_ones_are_listed():
    transport = github_api(
        files=[
            file_entry("docs.py", suppressed_patch()),
            file_entry("src/app.py", suppressed_patch(False)),
        ]
    )
    run_worker(transport, request_body())
    [review] = posted_reviews(transport)
    assert review["event"] == "REQUEST_CHANGES"
    assert "| 1 | AWS access key ID | `src/app.py` | 2 |" in review["body"]
    assert "1 finding(s) suppressed" in review["body"]
    assert gemini_calls(transport) == []  # an unsuppressed finding: no AI at all


def test_inline_mode_honours_suppressions_too():
    transport = github_api(files=[file_entry("docs.py", suppressed_patch())])
    status, body = Harness(transport)(api_event(pull_request_payload()))
    assert (status, body["reason"]) == (200, "COMMENT")


def test_an_invalid_allow_list_fails_closed():
    transport = github_api(files=[file_entry("docs.py", suppressed_patch(False))])
    _, result = run_worker(
        transport, request_body(), env={"MERGEGUARD_SCAN_ALLOW_PATHS": "**"}
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert transport.calls == []


# --- ST-06: log redaction, Phase 2 paths -------------------------------------


def test_st06_phase_2_logs_never_contain_secrets_or_diff_content():
    leaky = file_entry("settings.py", LEAKY_PATCH)
    suppressed = file_entry("docs.py", suppressed_patch())
    scenarios = [
        github_api(files=[leaky], reviews=[bot_review(11)]),
        github_api(files=[suppressed], reviews=[bot_review(11)]),
        github_api(files=[leaky], status_responses=(json_response(403, {}),)),
        github_api(files=[leaky], review_responses=(json_response(403, {}),)),
        github_api(
            files=[file_entry("a.py", CLEAN_PATCH)], pull=(pull_state(OLD_SHA),)
        ),
        github_api(
            files=[file_entry("a.py", CLEAN_PATCH)],
            gemini=(gemini_response(text="raw model chatter"),),
        ),
    ]
    forbidden = [
        TEST_WEBHOOK_SECRET,
        TEST_GITHUB_TOKEN,
        TEST_GEMINI_KEY,
        AWS_EXAMPLE_KEY_ID,
        sign(api_event(pull_request_payload())["body"].encode()),
        "AWS_KEY",
        "x = 2",
        "raw model chatter",
        "UNTRUSTED DATA",
    ]
    for transport in scenarios:
        harness = WorkerHarness(transport, env=STATUS_ENV)
        harness(request_body(), "garbage")
        sqs = FakeSqs()
        receiver = Harness(FakeTransport(), env=RECEIVER_ENV, sqs=sqs)
        receiver(api_event(pull_request_payload()))
        for output in (harness.logs.getvalue(), receiver.logs.getvalue()):
            for value in forbidden:
                assert value not in output
        for record in harness.records() + receiver.records():
            assert set(record) - {"ts", "level", "event"} <= ALLOWED_FIELDS


def test_a_dry_run_duplicate_heals_nothing():
    """The duplicate path finishes status and dismissals; in dry run it must not
    write either."""
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        reviews=[
            bot_review(11),
            bot_review(13, state="COMMENTED", commit_id=HEAD_SHA, kind="ai-review"),
        ],
    )
    env = {"MERGEGUARD_DRY_RUN": "true", **STATUS_ENV}
    _, result = run_worker(transport, request_body(), env=env)
    assert result == {"batchItemFailures": []}
    assert transport.requests("POST") == [] and dismissals(transport) == []


def test_worker_lambda_handler_wires_ssm_and_transport(monkeypatch):
    import worker_function
    from mergeguard.config import SecretStore

    ssm = FakeSsm()
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)])
    monkeypatch.setattr(worker_function, "_secret_store", None)
    monkeypatch.setattr(
        worker_function, "SecretStore", lambda prefix: SecretStore(prefix, lambda: ssm)
    )
    monkeypatch.setattr(worker_function, "urllib_transport", transport)
    monkeypatch.delenv("MERGEGUARD_PARAM_PREFIX", raising=False)
    event = {"Records": [{"messageId": "m-1", "body": request_body()}]}
    assert worker_function.lambda_handler(event, None) == {"batchItemFailures": []}
    assert worker_function.lambda_handler(event, None) == {"batchItemFailures": []}
    names = [name for call in ssm.calls for name in call["Names"]]
    assert names.count("/mergeguard/github_token") == 1  # the store is reused


def test_a_suppressed_private_key_never_reaches_the_ai():
    """The scanner flags only the PEM header line; the key material follows on
    lines that no rule flags, so it must be withheld together with the header."""
    from support import pem_header

    body = ["MIIEowIBAAKCAQEA" + "x" * 48, "yQ9fFakeKeyMaterial" + "z" * 45]
    end = "-----END RSA " + "PRIVATE " + "KEY-----"
    lines = [pem_header("RSA "), *body, end, "tail = 1"]
    patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)
    transport = github_api(files=[file_entry("tests/fixtures/dev.pem", patch)])
    _, result = run_worker(
        transport,
        request_body(),
        env={"MERGEGUARD_SCAN_ALLOW_PATHS": "tests/fixtures/**"},
    )
    assert result == {"batchItemFailures": []}
    [call] = gemini_calls(transport)
    sent = call.body.decode()
    assert not any(value in sent for value in [pem_header("RSA "), end, *body])
    assert "tail = 1" in sent
    assert "1 finding(s) suppressed" in posted_reviews(transport)[0]["body"]
