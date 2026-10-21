"""Review decision policy (FR-006, FR-010).

A pure function evaluated in code, never by a model. ``ReviewEvent`` has no
APPROVE member, so the policy cannot approve (PRD section 6.2).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from mergeguard.models import AIReview, SecretFinding

# Blocking threshold, open question OQ-04 (docs/PRD.md section 17). The
# documented default: only LOW risk with no bugs is a plain comment; MEDIUM and
# HIGH request changes.
COMMENT_ONLY_RISK_LEVELS = frozenset({"LOW"})
RISK_ORDER = ("LOW", "MEDIUM", "HIGH")


def comment_only_levels(block_on: str | None) -> frozenset[str]:
    """The risk levels that stay a plain comment when a repository sets the
    lowest level that blocks (FR-022). ``None`` keeps the default above. AI bugs
    and secret findings block whatever this says."""
    if block_on is None:
        return COMMENT_ONLY_RISK_LEVELS
    return frozenset(RISK_ORDER[: RISK_ORDER.index(block_on)])


class ReviewEvent(StrEnum):
    COMMENT = "COMMENT"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class Template(StrEnum):
    SECURITY = "T-SEC"
    AI_REVIEW = "T-AI"
    AI_UNAVAILABLE = "T-AIU"


@dataclass(frozen=True, slots=True)
class Decision:
    event: ReviewEvent | None  # None: post nothing
    template: Template | None
    reason: str


def decide(
    findings: Sequence[SecretFinding],
    ai_review: AIReview | None,
    *,
    ai_failure: str | None = None,
    has_patches: bool = True,
    comment_only: frozenset[str] = COMMENT_ONLY_RISK_LEVELS,
    ai_skip_reason: str | None = None,
) -> Decision:
    if not has_patches:
        return Decision(None, None, "no_patches")
    if findings:
        return Decision(
            ReviewEvent.REQUEST_CHANGES, Template.SECURITY, "secret_findings"
        )
    if ai_review is not None:
        if ai_review.risk_level in comment_only and ai_review.bugs_found == 0:
            return Decision(ReviewEvent.COMMENT, Template.AI_REVIEW, "low_risk")
        return Decision(ReviewEvent.REQUEST_CHANGES, Template.AI_REVIEW, "risk_or_bugs")
    if ai_skip_reason:  # the AI was not asked, by the repository's choice
        return Decision(None, None, ai_skip_reason)
    # Clean scan without a usable AI review: say so in a comment. An AI failure
    # never approves and never blocks (FR-008).
    return Decision(
        ReviewEvent.COMMENT, Template.AI_UNAVAILABLE, ai_failure or "unavailable"
    )
