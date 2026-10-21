"""Per-repository configuration, end to end (FR-022, T-603, ADR-0011), with a fake
GitHub and a mocked Gemini endpoint. No real call is made.

The security property under test: the file is read from the pull request's BASE
commit, never its head, and nothing in it can switch off the secret scan.
"""

import base64
import json
from datetime import UTC, datetime

import pytest

from mergeguard.logging_util import ALLOWED_FIELDS
from mergeguard.models import PullRequestRef
from mergeguard.render import MARKER_PREFIX
from mergeguard.review_queue import build_message
from support import (
    AWS_EXAMPLE_KEY_ID,
    BASE_SHA,
    BOT_LOGIN,
    GEMINI_ROUTE,
    HEAD_SHA,
    FakeTransport,
    Harness,
    WorkerHarness,
    api_event,
    file_entry,
    gemini_response,
    github_api,
    json_response,
    pull_request_payload,
    review_json,
)

CLEAN_PATCH = "@@ -1 +1 @@\n-x = 1\n+x = 2"
LEAKY = f"@@ -0,0 +1,2 @@\n+import os\n+AWS_KEY = '{AWS_EXAMPLE_KEY_ID}'"
CONFIG_ROUTE = r"/contents/\.github/mergeguard\.json\?ref="
CONFIG_ENV = {"MERGEGUARD_REPO_CONFIG": "true"}


def request_body() -> str:
    pr = PullRequestRef("owner/repo", 7, HEAD_SHA)
    return build_message(
        "72d3162e-cc78-11e3-81ab-4c9367dc0958",
        "opened",
        pr,
        datetime(2026, 10, 21, tzinfo=UTC),
    )


def config_file(data) -> object:
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    return json_response(
        200,
        {
            "type": "file",
            "encoding": "base64",
            "size": len(raw),
            "content": base64.encodebytes(raw).decode(),
        },
    )


def with_config(transport: FakeTransport, base_config=None, head_config=None):
    """Serve the file by ref. The base and head versions can differ, and only the
    base one may ever be read."""
    if head_config is not None:
        transport.route("GET", CONFIG_ROUTE + HEAD_SHA, config_file(head_config))
    if base_config is not None:
        transport.route("GET", CONFIG_ROUTE + BASE_SHA, config_file(base_config))
    transport.route("GET", CONFIG_ROUTE, json_response(404, {}))
    return transport


def run(transport, env=None):
    harness = WorkerHarness(transport, env={**CONFIG_ENV, **(env or {})})
    return harness, harness(request_body())


def posted(transport: FakeTransport) -> list[dict]:
    return [c.json() for c in transport.requests("POST", r"/pulls/\d+/reviews$")]


def gemini_calls(transport: FakeTransport) -> list:
    return transport.requests("POST", GEMINI_ROUTE)


def config_requests(transport: FakeTransport) -> list:
    return transport.requests("GET", r"/contents/")


def blocking_review(review_id: int = 11) -> dict:
    return {
        "id": review_id,
        "user": {"login": BOT_LOGIN},
        "commit_id": "f" * 40,
        "state": "CHANGES_REQUESTED",
        "body": f"{MARKER_PREFIX}kind=security sha=fffffff -->\n## review",
    }


# --- where the file is read from -----------------------------------------------


def test_the_config_is_read_from_the_base_commit_never_the_head():
    """The author of a PR cannot change the rules that apply to it."""
    transport = with_config(
        github_api(files=[file_entry("a.py", CLEAN_PATCH)]),
        head_config={"ai_review": False, "ignore_paths": ["**/*.py"]},  # hostile
    )
    _, result = run(transport)
    assert result == {"batchItemFailures": []}
    [call] = config_requests(transport)
    assert call.url.endswith(f"ref={BASE_SHA}") and HEAD_SHA not in call.url
    assert len(gemini_calls(transport)) == 1  # the head's file had no effect
    assert posted(transport)[0]["event"] == "COMMENT"


def test_without_a_config_file_the_defaults_apply_silently():
    transport = with_config(github_api(files=[file_entry("a.py", CLEAN_PATCH)]))
    _, result = run(transport)
    assert result == {"batchItemFailures": []}
    assert "mergeguard.json" not in posted(transport)[0]["body"]


def test_the_config_is_not_read_unless_the_operator_enabled_it():
    transport = with_config(
        github_api(files=[file_entry("a.py", CLEAN_PATCH)]), {"ai_review": False}
    )
    harness = WorkerHarness(transport)  # no MERGEGUARD_REPO_CONFIG
    harness(request_body())
    assert config_requests(transport) == []
    assert len(gemini_calls(transport)) == 1


def test_inline_mode_reads_the_base_commit_with_one_extra_pull_request_call():
    transport = with_config(
        github_api(files=[file_entry("a.py", CLEAN_PATCH)]), {"block_on": "HIGH"}
    )
    status, body = Harness(transport, env=CONFIG_ENV)(api_event(pull_request_payload()))
    assert (status, body["status"]) == (200, "reviewed")
    assert len(transport.requests("GET", r"/pulls/7$")) == 1
    assert config_requests(transport)[0].url.endswith(f"ref={BASE_SHA}")


# --- blocking thresholds --------------------------------------------------------


@pytest.mark.parametrize(
    ("risk", "bugs", "event"),
    [
        ("MEDIUM", 0, "COMMENT"),  # relaxed: the default would request changes
        ("HIGH", 0, "REQUEST_CHANGES"),
        ("MEDIUM", 2, "REQUEST_CHANGES"),  # AI bugs always block
        ("LOW", 0, "COMMENT"),
    ],
)
def test_block_on_high_relaxes_only_the_medium_case(risk, bugs, event):
    transport = with_config(
        github_api(
            files=[file_entry("a.py", CLEAN_PATCH)],
            gemini=(gemini_response(review_json(risk, bugs)),),
        ),
        {"block_on": "HIGH"},
    )
    run(transport)
    [review] = posted(transport)
    assert review["event"] == event
    assert (
        "Repository configuration `.github/mergeguard.json` applied: `block_on=HIGH`."
        in review["body"]
    )


def test_a_value_outside_the_operators_bounds_is_ignored_with_a_notice():
    transport = with_config(
        github_api(
            files=[file_entry("a.py", CLEAN_PATCH)],
            gemini=(gemini_response(review_json("MEDIUM", 0)),),
        ),
        {"block_on": "HIGH"},
    )
    harness, _ = run(transport, {"MERGEGUARD_REPO_CONFIG_BLOCK_LEVELS": "MEDIUM"})
    [review] = posted(transport)
    assert review["event"] == "REQUEST_CHANGES"  # the default still applies
    assert (
        "The repository configuration `.github/mergeguard.json` was ignored "
        "(out_of_bounds), so the default settings apply."
    ) in review["body"]
    [invalid] = [r for r in harness.records() if r["event"] == "repo_config.invalid"]
    assert (invalid["reason"], invalid["level"]) == ("out_of_bounds", "WARNING")


# --- invalid files ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"{broken", "invalid_json"),
        (json.dumps({"scan": False}).encode(), "unknown_key"),
        (json.dumps({"suppress": ["**"]}).encode(), "unknown_key"),
        (json.dumps({"ai_review": "yes"}).encode(), "invalid_field"),
        (json.dumps({"version": 9}).encode(), "unsupported_version"),
    ],
)
def test_an_invalid_file_falls_back_to_the_defaults_with_a_notice(content, reason):
    transport = with_config(
        github_api(files=[file_entry("a.py", CLEAN_PATCH)]), content
    )
    _, result = run(transport)
    assert result == {"batchItemFailures": []}  # never a failure
    assert f"was ignored ({reason})" in posted(transport)[0]["body"]
    assert len(gemini_calls(transport)) == 1  # the defaults: the AI review ran


def test_an_oversized_config_is_ignored_with_a_notice():
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)])
    transport.route(
        "GET",
        CONFIG_ROUTE,
        json_response(200, {"type": "file", "encoding": "none", "size": 5_000_000}),
    )
    run(transport)
    assert "was ignored (too_large)" in posted(transport)[0]["body"]


def test_a_token_that_cannot_read_the_file_just_gets_the_defaults():
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)])
    transport.route("GET", CONFIG_ROUTE, json_response(403, {}))
    _, result = run(transport)
    assert result == {"batchItemFailures": []}
    assert len(posted(transport)) == 1


def test_a_github_outage_while_reading_the_config_is_retried():
    transport = github_api(files=[file_entry("a.py", CLEAN_PATCH)])
    transport.route("GET", CONFIG_ROUTE, json_response(502, {}))
    _, result = run(transport)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert posted(transport) == []


# --- what the config can and cannot do to the AI and the scan ---------------------


def test_ai_review_false_keeps_the_diff_from_the_ai_and_posts_nothing():
    transport = with_config(
        github_api(
            files=[file_entry("a.py", CLEAN_PATCH)], reviews=[blocking_review()]
        ),
        {"ai_review": False},
    )
    harness, result = run(transport, {"MERGEGUARD_COMMIT_STATUS": "true"})
    assert result == {"batchItemFailures": []}
    assert gemini_calls(transport) == [] and posted(transport) == []
    assert any(
        r.get("reason") == "ai_disabled"
        for r in harness.records()
        if r["event"] == "review.skipped"
    )
    states = [c.json()["state"] for c in transport.requests("POST", r"/statuses/")]
    assert states == ["pending", "success"]
    # Nothing blocks, so the earlier blocking review is dismissed.
    assert len(transport.requests("PUT", r"/dismissals$")) == 1


def test_the_secret_scan_cannot_be_switched_off_by_the_config():
    transport = with_config(
        github_api(files=[file_entry("s.py", LEAKY)]),
        {"ai_review": False, "ignore_paths": ["**/*.py", "s.py"]},
    )
    run(transport)
    assert posted(transport)[0]["event"] == "REQUEST_CHANGES"
    assert gemini_calls(transport) == []


def test_ignore_paths_keep_those_files_from_the_ai_only():
    transport = with_config(
        github_api(
            files=[
                file_entry("docs/guide.md", "@@ -0,0 +1 @@\n+DOCS-ONLY-CONTENT"),
                file_entry("src/app.py", CLEAN_PATCH),
            ]
        ),
        {"ignore_paths": ["docs/**"]},
    )
    run(transport)
    [call] = gemini_calls(transport)
    sent = call.body.decode()
    assert "DOCS-ONLY-CONTENT" not in sent and "src/app.py" in sent
    assert "`ignore_paths=1 pattern(s)`" in posted(transport)[0]["body"]


def test_when_every_path_is_ignored_the_ai_is_not_called():
    transport = with_config(
        github_api(files=[file_entry("docs/guide.md", CLEAN_PATCH)]),
        {"ignore_paths": ["docs/**"]},
    )
    harness, _ = run(transport)
    assert gemini_calls(transport) == [] and posted(transport) == []
    assert "all_paths_ignored" in harness.logs.getvalue()


def test_an_ignored_path_is_still_scanned_for_secrets():
    transport = with_config(
        github_api(files=[file_entry("docs/keys.md", LEAKY)]),
        {"ignore_paths": ["docs/**"]},
    )
    run(transport)
    assert posted(transport)[0]["event"] == "REQUEST_CHANGES"


def test_the_config_cannot_add_suppressions():
    transport = with_config(
        github_api(files=[file_entry("s.py", LEAKY)]),
        {"scan_allow_paths": ["s.py"]},
    )
    run(transport)
    review = posted(transport)[0]
    assert review["event"] == "REQUEST_CHANGES"
    assert "was ignored (unknown_key)" in review["body"]


def test_config_reads_never_leak_into_the_logs():
    transport = with_config(
        github_api(files=[file_entry("a.py", CLEAN_PATCH)]),
        {"ignore_paths": ["secret-looking-dir/**"], "block_on": "HIGH"},
    )
    harness, _ = run(transport)
    assert "secret-looking-dir" not in harness.logs.getvalue()
    for record in harness.records():
        assert set(record) - {"ts", "level", "event"} <= ALLOWED_FIELDS


def test_a_non_utf8_or_non_object_file_does_not_crash_the_review():
    for content in (b"\xff\xfe\x00", b"[1, 2]", b"null"):
        transport = with_config(
            github_api(files=[file_entry("a.py", CLEAN_PATCH)]), content
        )
        _, result = run(transport)
        assert result == {"batchItemFailures": []}
        assert "was ignored (invalid_json)" in posted(transport)[0]["body"]
