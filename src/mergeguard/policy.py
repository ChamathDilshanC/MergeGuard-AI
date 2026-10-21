"""Review decision policy (FR-006, FR-010).

A pure function evaluated in code, never by a model. ``ReviewEvent`` has no
APPROVE member, so the policy cannot approve (PRD section 6.2).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from mergeguard.models import AIReview, SecretFinding


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
        if ai_review.risk_level == "LOW" and ai_review.bugs_found == 0:
            return Decision(ReviewEvent.COMMENT, Template.AI_REVIEW, "low_risk")
        return Decision(ReviewEvent.REQUEST_CHANGES, Template.AI_REVIEW, "risk_or_bugs")
    if ai_failure is not None:
        return Decision(ReviewEvent.COMMENT, Template.AI_UNAVAILABLE, ai_failure)
    # Scan is clean and no AI reviewer is wired in (M2 builds, before M3).
    return Decision(None, None, "ai_not_enabled")
