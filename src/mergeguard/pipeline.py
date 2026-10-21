"""Review pipeline for one pull request: files, duplicate check, secret scan,
AI review, decision, and review (FR-004 to FR-010, FR-014, FR-015).

The AI review runs only when the secret scan is clean (ADR-0004).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from mergeguard.ai_reviewer import AIOutcome
from mergeguard.github_client import GitHubClient, GitHubError
from mergeguard.logging_util import JsonLogger
from mergeguard.models import AIReview, ChangedFile, PullRequestRef
from mergeguard.policy import ReviewEvent, decide
from mergeguard.prompts import PROMPT_VERSION
from mergeguard.render import MARKER_PREFIX, Notices, render_review
from mergeguard.scanner import scan_files


class AIReviewer(Protocol):
    model: str

    def review(self, files: Sequence[ChangedFile]) -> AIOutcome: ...


@dataclass(frozen=True, slots=True)
class Outcome:
    """Result of handling a delivery, mapped to the HTTP response."""

    http_status: int
    status: str
    reason: str | None = None


class ReviewPipeline:
    def __init__(
        self,
        github: GitHubClient,
        reviewer: AIReviewer,
        logger: JsonLogger,
        *,
        max_files: int,
        dry_run: bool,
    ) -> None:
        self._github = github
        self._reviewer = reviewer
        self._log = logger
        self._max_files = max_files
        self._dry_run = dry_run

    def run(self, pr: PullRequestRef) -> Outcome:
        files, capped = self._github.list_pull_files(
            pr.repo, pr.number, self._max_files
        )
        self._log.log("files.fetched", file_count=len(files))
        reviewable = [changed for changed in files if changed.patch]
        if reviewable and self._already_reviewed(pr):
            self._log.log("review.duplicate")
            return Outcome(200, "duplicate")

        findings = scan_files(reviewable)
        rule_ids = sorted({finding.rule_id for finding in findings})
        self._log.log("scan.completed", rule_ids=rule_ids)
        ai = None
        if reviewable and not findings:  # never send a diff with secrets (ADR-0004)
            ai = self._ai_review(reviewable)
        decision = decide(
            findings,
            ai.review if ai else None,
            ai_failure=ai.failure if ai else None,
            has_patches=bool(reviewable),
        )
        if decision.event is None:
            self._log.log("review.skipped", reason=decision.reason)
            return Outcome(200, "skipped", decision.reason)

        partial_ai = ai is not None and ai.review is not None and ai.bundle.truncated
        notices = Notices(
            diff_limit=ai.bundle.max_chars if partial_ai else None,
            omitted_files=ai.bundle.omitted_files if partial_ai else (),
            files_cap=self._max_files if capped else None,
            no_patch_files=tuple(f.filename for f in files if not f.patch),
        )
        review: AIReview | None = ai.review if ai else None

        def render(fallback: bool) -> str:
            return render_review(
                decision,
                short_sha=pr.short_sha,
                findings=findings,
                ai_review=review,
                notices=notices,
                fallback=fallback,
            )

        if self._dry_run:
            self._log.log(
                "review.dry_run",
                decision=decision.event,
                rule_ids=rule_ids,
                body_length=len(render(False)),
            )
            return Outcome(200, "dry_run", decision.event)
        posted = self._post(pr, decision.event, render)
        return Outcome(200, "reviewed", posted)

    def _ai_review(self, files: list[ChangedFile]) -> AIOutcome:
        ai = self._reviewer.review(files)
        common = {
            "model": self._reviewer.model,
            "prompt_version": PROMPT_VERSION,
            "duration_ms": ai.duration_ms,
        }
        if ai.review is not None:
            self._log.log("ai.completed", risk_level=ai.review.risk_level, **common)
        else:  # the raw model output is never logged
            self._log.log("ai.failed", "WARNING", reason=ai.failure, **common)
        return ai

    def _already_reviewed(self, pr: PullRequestRef) -> bool:
        """True if the bot identity already reviewed this head SHA (FR-014).

        The author check stops anyone else from suppressing MergeGuard by pasting
        a copy of its marker into their own review.
        """
        login = self._github.authenticated_login()
        for review in self._github.list_reviews(pr.repo, pr.number):
            user = review.get("user")
            author = user.get("login") if isinstance(user, dict) else None
            body = review.get("body")
            if (
                review.get("commit_id") == pr.head_sha
                and author == login
                and isinstance(body, str)
                and MARKER_PREFIX in body
            ):
                return True
        return False

    def _post(
        self, pr: PullRequestRef, event: ReviewEvent, render: Callable[[bool], str]
    ) -> ReviewEvent:
        try:
            self._create(pr, event, render(False))
            posted = event
        except GitHubError as exc:
            # GitHub answers 422 when the token owner authored the PR (FR-010).
            own_pr_refusal = (
                exc.kind == "invalid_request" and event is ReviewEvent.REQUEST_CHANGES
            )
            if not own_pr_refusal:
                raise
            self._log.log("review.fallback_comment", http_status=exc.status)
            self._create(pr, ReviewEvent.COMMENT, render(True))
            posted = ReviewEvent.COMMENT
        self._log.log("review.posted", decision=posted)
        return posted

    def _create(self, pr: PullRequestRef, event: ReviewEvent, body: str) -> None:
        self._github.create_review(
            pr.repo, pr.number, commit_id=pr.head_sha, event=event, body=body
        )
