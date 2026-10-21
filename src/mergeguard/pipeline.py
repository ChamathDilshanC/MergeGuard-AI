"""Review pipeline for one pull request: files, duplicate check, secret scan,
AI review, decision, review, and the Phase 2 signals (FR-004 to FR-010, FR-014
to FR-019).

The AI review runs only when the secret scan is clean (ADR-0004), and lines the
scanner flagged but a suppression waved through are withheld from it (ADR-0008).

Commit status (FR-017) and stale-review dismissal (FR-018) are signals around
the review, not part of it. A permanent failure to send one (a token without the
permission, a protected branch) is logged and never stops the review. A transient
failure propagates, so the delivery is retried and the duplicate path finishes
the job.
"""

import re
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from mergeguard.ai_reviewer import AIOutcome
from mergeguard.github_client import GitHubClient, GitHubError
from mergeguard.logging_util import JsonLogger
from mergeguard.models import AIReview, ChangedFile, PullRequestRef
from mergeguard.policy import Decision, ReviewEvent, Template, decide
from mergeguard.prompts import PROMPT_VERSION
from mergeguard.render import (
    FALLBACK_CALLOUT,
    MARKER_PREFIX,
    STATUS_BLOCKED_AI,
    STATUS_BLOCKED_SECRETS,
    STATUS_CLEAR,
    STATUS_ERROR,
    STATUS_PENDING,
    Notices,
    dismissal_message,
    render_review,
)
from mergeguard.scanner import scan, withhold_suppressed

# GitHub conditions that a retry can fix; everything else is permanent.
TRANSIENT_KINDS = frozenset({"rate_limited", "unavailable"})

_KIND_RE = re.compile(re.escape(MARKER_PREFIX) + r"kind=([a-z-]+) ")


class AIReviewer(Protocol):
    model: str

    def review(self, files: Sequence[ChangedFile]) -> AIOutcome: ...


@dataclass(frozen=True, slots=True)
class Outcome:
    """Result of handling a delivery, mapped to the HTTP response."""

    http_status: int
    status: str
    reason: str | None = None


class Verdict(StrEnum):
    """What the review means for merging; drives the commit status."""

    CLEAR = "clear"
    BLOCKED_SECRETS = "blocked_secrets"
    BLOCKED_AI = "blocked_ai"


STATUS_FOR_VERDICT = {
    Verdict.CLEAR: STATUS_CLEAR,
    Verdict.BLOCKED_SECRETS: STATUS_BLOCKED_SECRETS,
    Verdict.BLOCKED_AI: STATUS_BLOCKED_AI,
}


class ReviewPipeline:
    def __init__(
        self,
        github: GitHubClient,
        reviewer: AIReviewer,
        logger: JsonLogger,
        *,
        max_files: int,
        dry_run: bool,
        commit_status: bool = False,
        allow_paths: Sequence[str] = (),
        verify_head: bool = False,
    ) -> None:
        self._github = github
        self._reviewer = reviewer
        self._log = logger
        self._max_files = max_files
        self._dry_run = dry_run
        self._commit_status = commit_status
        self._allow_paths = tuple(allow_paths)
        # The worker runs minutes after the webhook, so it re-checks the head
        # (PRD EC-07); the synchronous path acts on the delivery it just got.
        self._verify_head = verify_head
        self._reviews: list[Any] | None = None

    def run(self, pr: PullRequestRef) -> Outcome:
        self._reviews = None
        try:
            return self._run(pr)
        except Exception:
            # Do not leave `pending` behind. Best effort: the failure itself is
            # what the caller reports.
            with suppress(Exception):
                self._set_status(pr, STATUS_ERROR)
            raise

    def _run(self, pr: PullRequestRef) -> Outcome:
        stale = self._stale_reason(pr)
        if stale:
            return self._stale(stale)
        files, capped = self._github.list_pull_files(
            pr.repo, pr.number, self._max_files
        )
        self._log.log("files.fetched", file_count=len(files))
        reviewable = [changed for changed in files if changed.patch]
        if reviewable:
            existing = self._existing_review(pr)
            if existing is not None:
                self._log.log("review.duplicate")
                # A retry after a crash lands here: finish what the first run
                # did not (status, dismissals) from the review that exists.
                self._settle(pr, _verdict_of(existing))
                return Outcome(200, "duplicate")
            self._set_status(pr, STATUS_PENDING)

        result = scan(reviewable, self._allow_paths)
        findings = result.findings
        rule_ids = sorted({finding.rule_id for finding in findings})
        self._log.log(
            "scan.completed", rule_ids=rule_ids, suppressed=len(result.suppressed)
        )
        ai = None
        if reviewable and not findings:  # never send a diff with secrets (ADR-0004)
            ai = self._ai_review(withhold_suppressed(reviewable, result.suppressed))
        decision = decide(
            findings,
            ai.review if ai else None,
            ai_failure=ai.failure if ai else None,
            has_patches=bool(reviewable),
        )
        if decision.event is None:
            self._log.log("review.skipped", reason=decision.reason)
            self._set_status(pr, STATUS_CLEAR)
            return Outcome(200, "skipped", decision.reason)

        partial_ai = ai is not None and ai.review is not None and ai.bundle.truncated
        notices = Notices(
            diff_limit=ai.bundle.max_chars if partial_ai else None,
            omitted_files=ai.bundle.omitted_files if partial_ai else (),
            files_cap=self._max_files if capped else None,
            no_patch_files=tuple(f.filename for f in files if not f.patch),
            suppressed=tuple(result.suppressed),
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
        stale = self._stale_reason(pr)  # the AI call can take a while
        if stale:
            return self._stale(stale)
        posted = self._post(pr, decision.event, render)
        self._settle(pr, _verdict_for(decision))
        return Outcome(200, "reviewed", posted)

    def _stale_reason(self, pr: PullRequestRef) -> str | None:
        if not self._verify_head:
            return None
        current = self._github.pull_state(pr.repo, pr.number)
        if current.state != "open":
            return "pr_closed"
        if current.head_sha != pr.head_sha:
            return "stale_head"
        return None

    def _stale(self, reason: str) -> Outcome:
        self._log.log("review.skipped", reason=reason)
        return Outcome(200, "skipped", reason)

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

    def _reviews_once(self, pr: PullRequestRef) -> list[Any]:
        if self._reviews is None:
            self._reviews = list(self._github.list_reviews(pr.repo, pr.number))
        return self._reviews

    def _existing_review(self, pr: PullRequestRef) -> dict[str, Any] | None:
        """The bot's review of this head SHA, if there is one (FR-014).

        The author check stops anyone else from suppressing MergeGuard by pasting
        a copy of its marker into their own review.
        """
        login = self._github.authenticated_login()
        for review in self._reviews_once(pr):
            if (
                review.get("commit_id") == pr.head_sha
                and _author(review) == login
                and _has_marker(review)
            ):
                return review
        return None

    def _settle(self, pr: PullRequestRef, verdict: Verdict) -> None:
        """Publish what the review means: the commit status, and the end of any
        earlier blocking review when nothing blocks any more."""
        self._set_status(pr, STATUS_FOR_VERDICT[verdict])
        if verdict is Verdict.CLEAR:
            self._dismiss_stale(pr)

    def _set_status(self, pr: PullRequestRef, text: tuple[str, str]) -> None:
        if not self._commit_status or self._dry_run:
            return
        state, description = text
        sent = self._best_effort(
            "status.failed",
            lambda: self._github.create_status(
                pr.repo, pr.head_sha, state=state, description=description
            ),
        )
        if sent:
            self._log.log("status.set", commit_state=state)

    def _dismiss_stale(self, pr: PullRequestRef) -> None:
        """Dismiss the bot's earlier ``REQUEST_CHANGES`` reviews (FR-018): a
        later ``COMMENT`` review does not replace them, so they would keep
        blocking a pull request that was fixed (RB-11)."""
        if self._dry_run:
            return
        login = self._github.authenticated_login()
        message = dismissal_message(pr.short_sha)
        dismissed = 0
        for review in self._reviews_once(pr):
            review_id = review.get("id")
            if not (
                review.get("state") == "CHANGES_REQUESTED"
                and review.get("commit_id") != pr.head_sha
                and _author(review) == login
                and _has_marker(review)
                and type(review_id) is int
            ):
                continue
            sent = self._best_effort(
                "review.dismissal_failed",
                lambda review_id=review_id: self._github.dismiss_review(
                    pr.repo, pr.number, review_id, message
                ),
            )
            if not sent:
                break  # a missing permission or branch rule repeats for the rest
            dismissed += 1
        if dismissed:
            self._log.log("review.dismissed", dismissed=dismissed)

    def _best_effort(self, failure_event: str, action: Callable[[], None]) -> bool:
        try:
            action()
        except GitHubError as exc:
            if exc.kind in TRANSIENT_KINDS:
                raise
            self._log.log(
                failure_event, "WARNING", reason=exc.kind, http_status=exc.status
            )
            return False
        return True

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


def _verdict_for(decision: Decision) -> Verdict:
    """By the decision, not the posted event: a fallback comment on the author's
    own pull request still means changes are requested."""
    if decision.event is not ReviewEvent.REQUEST_CHANGES:
        return Verdict.CLEAR
    if decision.template is Template.SECURITY:
        return Verdict.BLOCKED_SECRETS
    return Verdict.BLOCKED_AI


def _verdict_of(review: dict[str, Any]) -> Verdict:
    """The verdict an existing MergeGuard review stands for."""
    body = review.get("body")
    text = body if isinstance(body, str) else ""
    kind = _KIND_RE.match(text)
    if kind and kind.group(1) == "security":
        return Verdict.BLOCKED_SECRETS
    if review.get("state") == "CHANGES_REQUESTED" or FALLBACK_CALLOUT in text:
        return Verdict.BLOCKED_AI
    return Verdict.CLEAR


def _author(review: dict[str, Any]) -> str | None:
    user = review.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) else None


def _has_marker(review: dict[str, Any]) -> bool:
    body = review.get("body")
    return isinstance(body, str) and MARKER_PREFIX in body
