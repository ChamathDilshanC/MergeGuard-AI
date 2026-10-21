"""In-memory types shared by the review pipeline (docs/ARCHITECTURE.md section 7)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    """The pull request a webhook delivery refers to."""

    repo: str  # "owner/name"
    number: int
    head_sha: str
    author_login: str
    draft: bool

    @property
    def short_sha(self) -> str:
        return self.head_sha[:7]


@dataclass(frozen=True, slots=True)
class ChangedFile:
    """One entry of the pull request files API. ``patch`` is None for binary or
    very large files."""

    filename: str
    status: str
    patch: str | None


@dataclass(frozen=True, slots=True)
class SecretFinding:
    """A secret-scanner hit. Deliberately holds no part of the matched value."""

    rule_id: str
    filename: str
    line: int


@dataclass(frozen=True, slots=True)
class AIReview:
    """Validated AI review output (docs/AI_SYSTEM.md section 5). Produced from M3."""

    risk_level: str
    bugs_found: int
    summary: str
    feedback_markdown: str
    model: str
