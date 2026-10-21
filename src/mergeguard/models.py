"""In-memory types shared by the review pipeline (docs/ARCHITECTURE.md section 7)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    """The pull request a webhook delivery refers to."""

    repo: str  # "owner/name"
    number: int
    head_sha: str
    author_login: str = ""  # not carried by queue messages (metadata only)
    draft: bool = False
    # Set by webhooks that GitHub sends for a GitHub App installation (FR-020).
    installation_id: int | None = None

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
class SuppressedFinding:
    """A scanner hit that an inline marker or the path allow-list suppressed
    (FR-019). Listed in the review, and its line is withheld from the AI."""

    rule_id: str
    filename: str
    line: int
    via: str  # "inline" or "path"


@dataclass(frozen=True, slots=True)
class DiffBundle:
    """The size-limited diff sent to the AI (docs/AI_SYSTEM.md section 6)."""

    text: str
    included_files: tuple[str, ...]  # sent in full
    omitted_files: tuple[str, ...]  # not sent, or only partly sent
    truncated: bool
    max_chars: int  # the budget the bundle was built under


@dataclass(frozen=True, slots=True)
class AIFinding:
    """One problem the model tied to a line (FR-021, prompt ``review-v2``). The
    pipeline posts it inline only if that line is in the diff the model saw."""

    file: str
    line: int
    message: str


@dataclass(frozen=True, slots=True)
class AIReview:
    """Validated AI review output (docs/AI_SYSTEM.md section 5)."""

    risk_level: str
    bugs_found: int
    summary: str
    feedback_markdown: str
    model: str
    findings: tuple[AIFinding, ...] = ()  # only with the review-v2 prompt
