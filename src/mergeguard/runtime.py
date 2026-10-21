"""Wiring shared by both Lambda entry points (the receiver in
``lambda_function`` and the SQS worker in ``worker_function``)."""

from collections.abc import Callable
from typing import Any

from mergeguard.ai_reviewer import GeminiReviewer
from mergeguard.config import Settings
from mergeguard.github_client import GitHubClient
from mergeguard.http import Transport
from mergeguard.logging_util import JsonLogger
from mergeguard.pipeline import ReviewPipeline

# Seconds kept free after a GitHub retry so the response is still returned.
TIME_SAFETY_MARGIN_SECONDS = 2.0


def seconds_left(context: Any) -> Callable[[], float | None]:
    remaining = getattr(context, "get_remaining_time_in_millis", None)

    def left() -> float | None:
        return None if remaining is None else remaining() / 1000

    return left


def time_budget(left: Callable[[], float | None]) -> Callable[[float], bool]:
    def has_time(seconds: float) -> bool:
        remaining = left()
        return remaining is None or remaining > seconds + TIME_SAFETY_MARGIN_SECONDS

    return has_time


def build_pipeline(
    settings: Settings,
    *,
    github_token: str,
    gemini_api_key: Callable[[], str],
    transport: Transport,
    context: Any,
    logger: JsonLogger,
    verify_head: bool,
) -> ReviewPipeline:
    left = seconds_left(context)
    github = GitHubClient(github_token, transport, has_time=time_budget(left))
    reviewer = GeminiReviewer(
        gemini_api_key,  # read from SSM only when an AI review runs
        transport,
        model=settings.gemini_model,
        max_diff_chars=settings.max_diff_chars,
        time_left=left,
    )
    return ReviewPipeline(
        github,
        reviewer,
        logger,
        max_files=settings.max_files,
        dry_run=settings.dry_run,
        commit_status=settings.commit_status,
        allow_paths=settings.scan_allow_paths,
        verify_head=verify_head,
    )
