"""Decision policy table (FR-010) and ST-09: APPROVE is never returned."""

import itertools

import pytest

from mergeguard.models import AIReview, SecretFinding
from mergeguard.policy import COMMENT_ONLY_RISK_LEVELS, ReviewEvent, Template, decide

FINDING = SecretFinding("aws-access-key-id", "a.py", 1)


def review(risk: str, bugs: int) -> AIReview:
    return AIReview(risk, bugs, "summary", "feedback", "gemini-2.5-flash")


COMMENT, REQUEST_CHANGES = ReviewEvent.COMMENT, ReviewEvent.REQUEST_CHANGES


@pytest.mark.parametrize(
    ("findings", "ai", "failure", "patches", "expected"),
    [
        ([], None, None, False, (None, None, "no_patches")),
        (
            [FINDING],
            None,
            None,
            True,
            (REQUEST_CHANGES, Template.SECURITY, "secret_findings"),
        ),
        # Secrets win even when an AI review exists (ADR-0004: AI is skipped).
        (
            [FINDING],
            review("LOW", 0),
            None,
            True,
            (REQUEST_CHANGES, Template.SECURITY, "secret_findings"),
        ),
        ([], review("LOW", 0), None, True, (COMMENT, Template.AI_REVIEW, "low_risk")),
        (
            [],
            review("LOW", 1),
            None,
            True,
            (REQUEST_CHANGES, Template.AI_REVIEW, "risk_or_bugs"),
        ),
        (
            [],
            review("MEDIUM", 0),
            None,
            True,
            (REQUEST_CHANGES, Template.AI_REVIEW, "risk_or_bugs"),
        ),
        (
            [],
            review("HIGH", 3),
            None,
            True,
            (REQUEST_CHANGES, Template.AI_REVIEW, "risk_or_bugs"),
        ),
        ([], None, "timeout", True, (COMMENT, Template.AI_UNAVAILABLE, "timeout")),
        # No review and no reason: still a comment, never silence or approval.
        ([], None, None, True, (COMMENT, Template.AI_UNAVAILABLE, "unavailable")),
    ],
    ids=[
        "no-patches",
        "secrets",
        "secrets-beat-ai",
        "ai-low",
        "ai-low-with-bugs",
        "ai-medium",
        "ai-high",
        "ai-unavailable",
        "no-ai-result",
    ],
)
def test_policy_table(findings, ai, failure, patches, expected):
    decision = decide(findings, ai, ai_failure=failure, has_patches=patches)
    assert (decision.event, decision.template, decision.reason) == expected


def test_oq04_default_threshold_only_low_risk_is_a_plain_comment():
    assert COMMENT_ONLY_RISK_LEVELS == {"LOW"}


@pytest.mark.parametrize("failure", ["timeout", "invalid_output", "blocked", "auth"])
def test_ai_failures_never_block_or_approve(failure):
    decision = decide([], None, ai_failure=failure)
    assert (decision.event, decision.template) == (COMMENT, Template.AI_UNAVAILABLE)


def test_st09_policy_never_approves():
    assert "APPROVE" not in ReviewEvent.__members__
    ai_options = [None] + [
        review(risk, bugs)
        for risk in ("LOW", "MEDIUM", "HIGH", "bogus")
        for bugs in (0, 2)
    ]
    for findings, ai, failure, patches in itertools.product(
        ([], [FINDING]), ai_options, (None, "timeout"), (True, False)
    ):
        decision = decide(findings, ai, ai_failure=failure, has_patches=patches)
        assert decision.event in (None, COMMENT, REQUEST_CHANGES)
