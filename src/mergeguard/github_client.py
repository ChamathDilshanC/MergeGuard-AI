"""GitHub REST client for pull request files and reviews (FR-004, FR-010, FR-014).

Error mapping follows docs/API_SPEC.md section 3.2. ``GitHubError.kind`` doubles
as the suffix of the ``github.*`` log events.
"""

import json
import time
from collections.abc import Callable, Mapping
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
    ) -> None:
        self._token = token
        self._transport = transport
        self._api_url = api_url
        self._timeout = timeout
        self._retry_delay = retry_delay
        self._sleep = sleep
        self._has_time = has_time
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
        if self._login is None:
            data = _json(self._request("GET", f"{self._api_url}/user"))
            login = data.get("login") if isinstance(data, dict) else None
            if not isinstance(login, str) or not login:
                raise GitHubError("unexpected", 200)
            self._login = login
        return self._login

    def create_review(
        self, repo: str, number: int, *, commit_id: str, event: str, body: str
    ) -> None:
        if event not in ALLOWED_REVIEW_EVENTS:
            raise ValueError(f"review event {event!r} is not allowed")
        payload = {"commit_id": commit_id, "event": event, "body": body}
        self._request("POST", f"{self._pull_url(repo, number)}/reviews", payload)

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
