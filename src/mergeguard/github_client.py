"""GitHub REST client for pull request files and reviews (FR-004, FR-010, FR-014).

Error mapping follows docs/API_SPEC.md section 3.2. ``GitHubError.kind`` doubles
as the suffix of the ``github.*`` log events.
"""

import base64
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from mergeguard.http import HttpResponse, Transport, TransportError
from mergeguard.models import ChangedFile

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "MergeGuard-AI-Bot"
TIMEOUT_SECONDS = 10.0
RETRY_DELAY_SECONDS = 1.0
PER_PAGE = 100
MAX_REVIEW_PAGES = 10
# Defence in depth: the client itself refuses APPROVE (PRD section 6.2).
ALLOWED_REVIEW_EVENTS = frozenset({"COMMENT", "REQUEST_CHANGES"})
STATUS_CONTEXT = "mergeguard/review"  # FR-017; branch protection matches this name
STATUS_STATES = frozenset({"pending", "success", "failure", "error"})
MAX_STATUS_DESCRIPTION = 140  # GitHub's limit


@dataclass(frozen=True, slots=True)
class PullState:
    """What the worker re-checks before spending an AI call or posting."""

    head_sha: str
    state: str  # "open" or "closed"
    # The commit the pull request is based on. Repository configuration is read
    # from here, never from the head, so a PR cannot edit its own rules (ADR-0011).
    base_sha: str | None = None


class FileTooLarge(Exception):
    """A file exists but is larger than the caller will read."""


class GitHubError(Exception):
    def __init__(self, kind: str, status: int | None) -> None:
        super().__init__(f"{kind} (HTTP {status})")
        self.kind = kind
        self.status = status


def classify_status(response: HttpResponse) -> str | None:
    """Map an HTTP response to an error kind, or None for success."""
    status = response.status
    if 200 <= status < 300:
        return None
    if status == 401:
        return "auth_failed"
    if status == 429 or (status == 403 and _is_rate_limited(response.headers)):
        return "rate_limited"
    if status in (403, 404):
        return "forbidden"
    if status == 422:
        return "invalid_request"
    if status >= 500:
        return "unavailable"
    return "unexpected"


def _always(_seconds: float) -> bool:
    return True


class GitHubClient:
    def __init__(
        self,
        token: str,
        transport: Transport,
        *,
        api_url: str = API_URL,
        timeout: float = TIMEOUT_SECONDS,
        retry_delay: float = RETRY_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        has_time: Callable[[float], bool] = _always,
        login: Callable[[], str] | None = None,
    ) -> None:
        self._token = token
        self._transport = transport
        self._api_url = api_url
        self._timeout = timeout
        self._retry_delay = retry_delay
        self._sleep = sleep
        self._has_time = has_time
        # App installation tokens cannot call GET /user, so App mode supplies
        # the bot login instead (app_auth.AppAuth.bot_login).
        self._login_resolver = login
        self._login: str | None = None

    def list_pull_files(
        self, repo: str, number: int, max_files: int
    ) -> tuple[list[ChangedFile], bool]:
        """Return up to ``max_files`` changed files and whether the list was cut."""
        files: list[ChangedFile] = []
        page = 1
        while True:
            url = (
                f"{self._pull_url(repo, number)}/files?per_page={PER_PAGE}&page={page}"
            )
            items = self._get_list(url)
            files.extend(_changed_file(item) for item in items)
            if len(items) < PER_PAGE:
                return files[:max_files], len(files) > max_files
            if len(files) >= max_files:
                return files[:max_files], True
            page += 1

    def list_reviews(self, repo: str, number: int) -> list[Mapping[str, Any]]:
        reviews: list[Mapping[str, Any]] = []
        for page in range(1, MAX_REVIEW_PAGES + 1):
            query = f"per_page={PER_PAGE}&page={page}"
            items = self._get_list(f"{self._pull_url(repo, number)}/reviews?{query}")
            reviews.extend(item for item in items if isinstance(item, dict))
            if len(items) < PER_PAGE:
                break
        return reviews

    def authenticated_login(self) -> str:
        """Login of the token owner, cached for the client's lifetime."""
        if self._login is None and self._login_resolver is not None:
            self._login = self._login_resolver()
        if self._login is None:
            data = _json(self._request("GET", f"{self._api_url}/user"))
            login = data.get("login") if isinstance(data, dict) else None
            if not isinstance(login, str) or not login:
                raise GitHubError("unexpected", 200)
            self._login = login
        return self._login

    def create_review(
        self,
        repo: str,
        number: int,
        *,
        commit_id: str,
        event: str,
        body: str,
        comments: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Post a review. ``comments`` are inline comments (FR-021), each with
        ``path``, ``line``, ``side`` and ``body``, created with the review so
        they appear together or not at all."""
        if event not in ALLOWED_REVIEW_EVENTS:
            raise ValueError(f"review event {event!r} is not allowed")
        payload: dict[str, Any] = {
            "commit_id": commit_id,
            "event": event,
            "body": body,
        }
        if comments:
            payload["comments"] = [dict(comment) for comment in comments]
        self._request("POST", f"{self._pull_url(repo, number)}/reviews", payload)

    def pull_state(self, repo: str, number: int) -> PullState:
        """Current head SHA and open/closed state of the pull request."""
        data = _json(self._request("GET", self._pull_url(repo, number)))
        head = data.get("head") if isinstance(data, dict) else None
        sha = head.get("sha") if isinstance(head, dict) else None
        state = data.get("state") if isinstance(data, dict) else None
        if not isinstance(sha, str) or not isinstance(state, str):
            raise GitHubError("unexpected", 200)
        base = data.get("base")
        base_sha = base.get("sha") if isinstance(base, dict) else None
        return PullState(sha, state, base_sha if isinstance(base_sha, str) else None)

    def get_file(
        self, repo: str, path: str, ref: str, max_bytes: int = 1_000_000
    ) -> bytes | None:
        """The content of ``path`` at commit ``ref``, or None when GitHub has no
        such file (or a directory) or does not let this token read it. A repository
        without the file is the normal case, so neither is an error. A file above
        ``max_bytes`` raises ``FileTooLarge``; GitHub returns no content at all
        above 1 MB."""
        owner, name = repo.split("/")
        url = (
            f"{self._api_url}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}"
        )
        try:
            data = _json(self._request("GET", url))
        except GitHubError as exc:
            if exc.kind == "forbidden":  # 403 or 404
                return None
            raise
        if not isinstance(data, dict) or data.get("type") not in (None, "file"):
            return None
        size = data.get("size")
        if (isinstance(size, int) and size > max_bytes) or data.get(
            "encoding"
        ) == "none":
            raise FileTooLarge
        content = data.get("content")
        if data.get("encoding") != "base64" or not isinstance(content, str):
            return None
        try:
            return base64.b64decode("".join(content.split()), validate=True)
        except ValueError as exc:
            raise GitHubError("unexpected", 200) from exc

    def create_status(
        self, repo: str, sha: str, *, state: str, description: str
    ) -> None:
        """Set the ``mergeguard/review`` commit status (FR-017)."""
        if state not in STATUS_STATES:
            raise ValueError(f"commit status state {state!r} is not allowed")
        owner, name = repo.split("/")
        url = (
            f"{self._api_url}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/statuses/{quote(sha, safe='')}"
        )
        payload = {
            "state": state,
            "context": STATUS_CONTEXT,
            "description": description[:MAX_STATUS_DESCRIPTION],
        }
        self._request("POST", url, payload)

    def dismiss_review(
        self, repo: str, number: int, review_id: int, message: str
    ) -> None:
        """Dismiss a review, with a message that explains why (FR-018)."""
        url = f"{self._pull_url(repo, number)}/reviews/{int(review_id)}/dismissals"
        self._request("PUT", url, {"message": message, "event": "DISMISS"})

    def _pull_url(self, repo: str, number: int) -> str:
        owner, name = repo.split("/")
        return (
            f"{self._api_url}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/pulls/{number}"
        )

    def _get_list(self, url: str) -> list[Any]:
        data = _json(self._request("GET", url))
        if not isinstance(data, list):
            raise GitHubError("unexpected", 200)
        return data

    def _request(
        self, method: str, url: str, payload: Mapping[str, Any] | None = None
    ) -> HttpResponse:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            attempt += 1
            try:
                response: HttpResponse | None = self._transport(
                    method, url, headers, body, self._timeout
                )
                kind = classify_status(response)
            except TransportError:
                response, kind = None, "unavailable"
            if kind is None and response is not None:
                return response
            if attempt == 1 and self._should_retry(method, kind):
                self._sleep(self._retry_delay)
                continue
            raise GitHubError(
                kind or "unexpected", response.status if response else None
            )

    def _should_retry(self, method: str, kind: str | None) -> bool:
        # A POST that failed with 5xx or a dropped connection may still have been
        # applied, so only rate limits (never applied) are retried for writes.
        retryable = kind == "rate_limited" or (
            method == "GET" and kind == "unavailable"
        )
        return retryable and self._has_time(self._retry_delay + self._timeout)


def _is_rate_limited(headers: Mapping[str, str]) -> bool:
    return headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers


def _json(response: HttpResponse) -> Any:
    try:
        return json.loads(response.body)
    except ValueError as exc:
        raise GitHubError("unexpected", response.status) from exc


def _changed_file(item: Any) -> ChangedFile:
    if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
        raise GitHubError("unexpected", 200)
    patch = item.get("patch")
    return ChangedFile(
        filename=item["filename"],
        status=str(item.get("status", "")),
        patch=patch if isinstance(patch, str) else None,
    )
