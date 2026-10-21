"""Review pipeline for one pull request: files, duplicate check, secret scan,
decision, and review (FR-004, FR-006, FR-010, FR-014, FR-015).

M2 has no AI step: a clean scan is logged and skipped (``ai_not_enabled``).
The AI review is added in M3 between the scan and the decision.
"""

from collections.abc import Callable
from dataclasses import dataclass

from mergeguard.github_client import GitHubClient, GitHubError
from mergeguard.logging_util import JsonLogger
from mergeguard.models import PullRequestRef
from mergeguard.policy import ReviewEvent, decide
from mergeguard.render import MARKER_PREFIX, Notices, render_review
from mergeguard.scanner import scan_files


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
        logger: JsonLogger,
        *,
        max_files: int,
        dry_run: bool,
    ) -> None:
        self._github = github
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
        decision = decide(findings, None, has_patches=bool(reviewable))
        if decision.event is None:
            self._log.log("review.skipped", reason=decision.reason)
            return Outcome(200, "skipped", decision.reason)

        notices = Notices(
            files_cap=self._max_files if capped else None,
            no_patch_files=tuple(f.filename for f in files if not f.patch),
        )

        def render(fallback: bool) -> str:
            return render_review(
                decision,
                short_sha=pr.short_sha,
                findings=findings,
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
