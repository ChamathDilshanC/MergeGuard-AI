"""Inline line-level comments, end to end (FR-021, T-602) with a fake GitHub and a
mocked Gemini endpoint.

The properties under test: comments are anchored only to lines that are present in
the diff (and for AI findings, in the diff the model was actually sent), a secret
comment never contains the value, and the verdict survives GitHub refusing the
comments.
"""

import base64
import json
from datetime import UTC, datetime

import pytest

from mergeguard.models import PullRequestRef
from mergeguard.prompts import SYSTEM_INSTRUCTION
from mergeguard.render import FALLBACK_CALLOUT
from mergeguard.review_queue import build_message
from support import (
    AWS_EXAMPLE_KEY_ID,
    BASE_SHA,
    GEMINI_ROUTE,
    HEAD_SHA,
    FakeTransport,
    WorkerHarness,
    fake_github_token,
    file_entry,
    gemini_response,
    github_api,
    json_response,
    review_json,
)

LEAKY = f"@@ -0,0 +1,2 @@\n+import os\n+AWS_KEY = '{AWS_EXAMPLE_KEY_ID}'"
CLEAN_PATCH = "@@ -1,3 +1,4 @@\n context line\n-old\n+new line one\n+new line two\n tail context"  # new-file lines: 1 context, 2 added, 3 added, 4 context
MARKER = "mergeguard:ignore-secret"
SECRETS = {"MERGEGUARD_INLINE_COMMENTS": "secrets"}
ALL = {"MERGEGUARD_INLINE_COMMENTS": "all"}


def request_body() -> str:
    pr = PullRequestRef("owner/repo", 7, HEAD_SHA)
    return build_message(
        "72d3162e-cc78-11e3-81ab-4c9367dc0958",
        "opened",
        pr,
        datetime(2026, 10, 21, tzinfo=UTC),
    )


def run(transport, env=None):
    harness = WorkerHarness(transport, env=env)
    return harness, harness(request_body())


def posted(transport: FakeTransport) -> list[dict]:
    return [c.json() for c in transport.requests("POST", r"/pulls/\d+/reviews$")]


def gemini_calls(transport: FakeTransport) -> list:
    return transport.requests("POST", GEMINI_ROUTE)


def with_findings(findings, risk="MEDIUM", bugs=1):
    return gemini_response({**review_json(risk, bugs), "findings": findings})


# --- secret findings -------------------------------------------------------------


def test_secret_findings_get_an_inline_comment_without_the_value():
    transport = github_api(files=[file_entry("s.py", LEAKY)])
    _, result = run(transport, SECRETS)
    assert result == {"batchItemFailures": []}
    [review] = posted(transport)
    assert review["event"] == "REQUEST_CHANGES"
    [comment] = review["comments"]
    assert (comment["path"], comment["line"], comment["side"]) == ("s.py", 2, "RIGHT")
    assert (
        "AWS access key ID" in comment["body"] and "Revoke or rotate" in comment["body"]
    )
    assert AWS_EXAMPLE_KEY_ID not in json.dumps(review)


def test_inline_comments_are_off_by_default():
    transport = github_api(files=[file_entry("s.py", LEAKY)])
    run(transport)
    assert "comments" not in posted(transport)[0]


def test_two_rules_on_one_line_make_one_comment():
    patch = f"@@ -0,0 +1 @@\n+a = '{AWS_EXAMPLE_KEY_ID}'; b = '{fake_github_token()}'"
    transport = github_api(files=[file_entry("s.py", patch)])
    run(transport, SECRETS)
    [comment] = posted(transport)[0]["comments"]
    assert comment["line"] == 1
    assert "AWS access key ID, GitHub token" in comment["body"]


def test_at_most_twenty_five_secret_comments_are_posted():
    lines = [f"+k{i} = '{AWS_EXAMPLE_KEY_ID}'" for i in range(30)]
    patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(lines)
    transport = github_api(files=[file_entry("s.py", patch)])
    run(transport, SECRETS)
    review = posted(transport)[0]
    assert len(review["comments"]) == 25
    assert "…and 5 more." in review["body"]  # the summary still lists them all


def test_suppressed_findings_get_no_comment():
    patch = f"@@ -0,0 +1 @@\n+k = '{AWS_EXAMPLE_KEY_ID}'  # {MARKER}"
    transport = github_api(files=[file_entry("s.py", patch)])
    run(transport, SECRETS)
    assert all("comments" not in review for review in posted(transport))


def test_a_dry_run_posts_nothing():
    transport = github_api(files=[file_entry("s.py", LEAKY)])
    run(transport, {**SECRETS, "MERGEGUARD_DRY_RUN": "true"})
    assert posted(transport) == []


# --- GitHub refuses the comments ---------------------------------------------------


def test_refused_comments_are_dropped_and_the_verdict_stays():
    transport = github_api(
        files=[file_entry("s.py", LEAKY)],
        review_responses=(json_response(422, {}), json_response(200, {})),
    )
    harness, result = run(transport, SECRETS)
    assert result == {"batchItemFailures": []}
    first, second = posted(transport)
    assert first["event"] == second["event"] == "REQUEST_CHANGES"  # not downgraded
    assert "comments" in first and "comments" not in second
    assert "review.inline_dropped" in harness.events()
    done = [r for r in harness.records() if r["event"] == "review.posted"]
    assert done[-1]["inline_comments"] == 0


def test_the_own_pr_fallback_keeps_the_inline_comments():
    """422, 422 (still refused without comments: it is the own-PR case), then the
    comment fallback with the comments attached."""
    transport = github_api(
        files=[file_entry("s.py", LEAKY)],
        review_responses=(
            json_response(422, {}),
            json_response(422, {}),
            json_response(200, {}),
        ),
    )
    harness, result = run(transport, SECRETS)
    assert result == {"batchItemFailures": []}
    reviews = posted(transport)
    assert [r["event"] for r in reviews] == [
        "REQUEST_CHANGES",
        "REQUEST_CHANGES",
        "COMMENT",
    ]
    assert [("comments" in r) for r in reviews] == [True, False, True]
    assert FALLBACK_CALLOUT in reviews[2]["body"]
    assert "review.fallback_comment" in harness.events()


def test_everything_refused_is_an_error():
    transport = github_api(
        files=[file_entry("s.py", LEAKY)], review_responses=(json_response(422, {}),)
    )
    _, result = run(transport, SECRETS)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert len(posted(transport)) == 4  # RC+c, RC, COMMENT+c, COMMENT


def test_other_errors_are_not_retried_as_a_variant():
    transport = github_api(
        files=[file_entry("s.py", LEAKY)], review_responses=(json_response(403, {}),)
    )
    _, result = run(transport, SECRETS)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert len(posted(transport)) == 1


# --- AI findings (prompt review-v2) ----------------------------------------------


def test_ai_findings_are_posted_only_for_lines_in_the_diff():
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)],
        gemini=(
            with_findings(
                [
                    {
                        "file": "a.py",
                        "line": 3,
                        "message": "Validate this input @octocat.",
                    },
                    {"file": "a.py", "line": 1, "message": "A context line is fine."},
                    {"file": "a.py", "line": 9, "message": "Not in the diff."},
                    {
                        "file": "other.py",
                        "line": 2,
                        "message": "Not a file in the diff.",
                    },
                    {"file": "a.py", "line": 0, "message": "Line zero."},
                    {"file": "a.py", "line": "3", "message": "Line is a string."},
                    {"file": "a.py", "line": 2, "message": "   "},
                    "not an object",
                ]
            ),
        ),
    )
    harness, _ = run(transport, ALL)
    [review] = posted(transport)
    assert [(c["path"], c["line"]) for c in review["comments"]] == [
        ("a.py", 3),
        ("a.py", 1),
    ]
    assert review["comments"][0]["body"] == "🤖 Validate this input @​octocat."
    assert review["event"] == "REQUEST_CHANGES"  # the policy ignores findings
    [done] = [r for r in harness.records() if r["event"] == "ai.completed"]
    assert done["prompt_version"] == "review-v2"


def test_the_findings_prompt_is_sent_only_in_all_mode():
    for env, version, expects_findings in (
        (ALL, "review-v2", True),
        (SECRETS, "review-v1", False),
    ):
        transport = github_api(
            files=[file_entry("a.py", CLEAN_PATCH)],
            gemini=(with_findings([{"file": "a.py", "line": 3, "message": "x"}]),),
        )
        harness, _ = run(transport, env)
        sent = json.loads(gemini_calls(transport)[0].body)
        instruction = sent["systemInstruction"]["parts"][0]["text"]
        properties = sent["generationConfig"]["responseSchema"]["properties"]
        assert ("findings" in properties) is expects_findings
        assert (instruction != SYSTEM_INSTRUCTION) is expects_findings
        assert ("findings:" in instruction) is expects_findings
        if not expects_findings:
            assert instruction == SYSTEM_INSTRUCTION  # the evaluated prompt, unchanged
            assert all(
                "comments" not in r for r in posted(transport)
            )  # findings ignored
        [done] = [r for r in harness.records() if r["event"] == "ai.completed"]
        assert done["prompt_version"] == version


def test_a_line_withheld_from_the_ai_is_never_anchored():
    patch = f"@@ -0,0 +1,2 @@\n+safe = 1\n+k = '{AWS_EXAMPLE_KEY_ID}'  # {MARKER}"
    transport = github_api(
        files=[file_entry("a.py", patch)],
        gemini=(
            with_findings(
                [
                    {"file": "a.py", "line": 1, "message": "On the visible line."},
                    {"file": "a.py", "line": 2, "message": "On the withheld line."},
                ]
            ),
        ),
    )
    run(transport, ALL)
    assert [c["line"] for c in posted(transport)[0]["comments"]] == [1]
    assert AWS_EXAMPLE_KEY_ID not in json.dumps(posted(transport))


def test_a_file_the_model_never_saw_in_full_gets_no_ai_comment():
    transport = github_api(
        files=[
            file_entry("small.py", CLEAN_PATCH),
            file_entry("big.py", "@@ -0,0 +1 @@\n+" + "y" * 3000),
        ],
        gemini=(
            with_findings(
                [
                    {"file": "big.py", "line": 1, "message": "Truncated file."},
                    {"file": "small.py", "line": 2, "message": "Sent in full."},
                ]
            ),
        ),
    )
    run(transport, {**ALL, "MERGEGUARD_MAX_DIFF_CHARS": "1000"})
    assert [(c["path"], c["line"]) for c in posted(transport)[0]["comments"]] == [
        ("small.py", 2)
    ]


def test_at_most_ten_ai_comments_are_posted():
    findings = [
        {"file": "a.py", "line": 1 + (i % 4), "message": f"m{i}"} for i in range(40)
    ]
    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)], gemini=(with_findings(findings),)
    )
    run(transport, ALL)
    assert len(posted(transport)[0]["comments"]) <= 4  # one per anchored line


def test_a_failed_ai_review_posts_no_ai_comments():
    from mergeguard.http import TransportError

    transport = github_api(
        files=[file_entry("a.py", CLEAN_PATCH)], gemini=(TransportError("timeout"),)
    )
    run(transport, ALL)
    assert "comments" not in posted(transport)[0]


def test_ai_comments_follow_secret_comments_in_all_mode_only_when_the_scan_is_clean():
    """With a finding there is no AI call at all, so only the secret comment."""
    transport = github_api(files=[file_entry("s.py", LEAKY)])
    run(transport, ALL)
    assert gemini_calls(transport) == []
    assert len(posted(transport)[0]["comments"]) == 1


# --- the repository can only lower the mode ----------------------------------------


def config_route(transport: FakeTransport, data) -> FakeTransport:
    raw = json.dumps(data).encode()
    transport.route(
        "GET",
        r"/contents/\.github/mergeguard\.json\?ref=" + BASE_SHA,
        json_response(
            200,
            {
                "type": "file",
                "encoding": "base64",
                "size": len(raw),
                "content": base64.b64encode(raw).decode(),
            },
        ),
    )
    return transport


@pytest.mark.parametrize(
    ("operator", "wanted", "has_comments", "notice"),
    [
        ("secrets", "off", False, "inline_comments=off"),
        ("all", "secrets", True, "inline_comments=secrets"),
        ("secrets", "all", True, "was ignored (out_of_bounds)"),  # above the operator
    ],
)
def test_the_repository_can_lower_but_never_raise_the_mode(
    operator, wanted, has_comments, notice
):
    transport = config_route(
        github_api(files=[file_entry("s.py", LEAKY)]), {"inline_comments": wanted}
    )
    run(
        transport,
        {"MERGEGUARD_INLINE_COMMENTS": operator, "MERGEGUARD_REPO_CONFIG": "true"},
    )
    review = posted(transport)[0]
    assert ("comments" in review) is has_comments
    assert notice in review["body"]
