"""Wiring shared by both Lambda entry points (the receiver in
``lambda_function`` and the SQS worker in ``worker_function``)."""

import time
from collections.abc import Callable, MutableMapping
from typing import Any

from mergeguard.ai_reviewer import GeminiReviewer
from mergeguard.app_auth import AppAuth, installation_permissions
from mergeguard.config import ConfigError, SecretStore, Settings
from mergeguard.github_client import GitHubClient
from mergeguard.http import Transport
from mergeguard.logging_util import JsonLogger
from mergeguard.models import PullRequestRef
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


def github_identity(
    settings: Settings,
    store: SecretStore,
    transport: Transport,
    pr: PullRequestRef,
    *,
    token: str | None = None,
    cache: MutableMapping[Any, Any] | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[str, Callable[[], str] | None]:
    """The GitHub credential for one pull request, and how to find the login that
    owns the reviews.

    With ``MERGEGUARD_GITHUB_APP_ID`` set (FR-020) this mints an installation token
    for the pull request's repository and never reads the personal access token.
    Otherwise it is the personal access token (``token`` if the caller already
    loaded it), and the login comes from ``GET /user``.
    """
    if settings.github_app_id is None:
        return (token if token is not None else store.github_token()), None
    if pr.installation_id is None:
        raise ConfigError(
            "GitHub App mode needs a webhook that carries an installation ID"
        )
    auth = AppAuth(
        settings.github_app_id,
        store.github_app_private_key,
        transport,
        installation_permissions(
            statuses=settings.commit_status, contents=settings.repo_config
        ),
        cache=cache,
        clock=clock,
    )
    return auth.installation_token(pr.repo, pr.installation_id), auth.bot_login


def build_pipeline(
    settings: Settings,
    *,
    github_token: str,
    login: Callable[[], str] | None = None,
    gemini_api_key: Callable[[], str],
    transport: Transport,
    context: Any,
    logger: JsonLogger,
    verify_head: bool,
) -> ReviewPipeline:
    left = seconds_left(context)
    github = GitHubClient(
        github_token, transport, has_time=time_budget(left), login=login
    )
    reviewer = GeminiReviewer(
        gemini_api_key,  # read from SSM only when an AI review runs
        transport,
        model=settings.gemini_model,
        max_diff_chars=settings.max_diff_chars,
        time_left=left,
        findings=settings.inline_comments == "all",  # prompt review-v2 (FR-021)
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
        inline_comments=settings.inline_comments,
        use_repo_config=settings.repo_config,
        block_levels=settings.repo_config_block_levels,
    )
