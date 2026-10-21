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
) -> Decision:
    if not has_patches:
        return Decision(None, None, "no_patches")
    if findings:
        return Decision(
            ReviewEvent.REQUEST_CHANGES, Template.SECURITY, "secret_findings"
        )
    if ai_review is not None:
        if (
            ai_review.risk_level in COMMENT_ONLY_RISK_LEVELS
            and ai_review.bugs_found == 0
        ):
            return Decision(ReviewEvent.COMMENT, Template.AI_REVIEW, "low_risk")
        return Decision(ReviewEvent.REQUEST_CHANGES, Template.AI_REVIEW, "risk_or_bugs")
    # Clean scan without a usable AI review: say so in a comment. An AI failure
    # never approves and never blocks (FR-008).
    return Decision(
        ReviewEvent.COMMENT, Template.AI_UNAVAILABLE, ai_failure or "unavailable"
    )
