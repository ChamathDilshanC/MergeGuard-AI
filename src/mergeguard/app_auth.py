"""GitHub App authentication: installation access tokens (FR-020, ADR-0010).

The worker signs a short-lived JWT with the App's private key
(``rsa_jwt``), exchanges it for an installation token, and uses that token for
every GitHub call. The token is minted **for one repository** and only the
permissions MergeGuard needs, so a leaked token reaches one repository for at most
an hour. Reviews then appear as ``<app-slug>[bot]``, which also ends the own-PR
``422`` (FR-010) for good.

Tokens are cached per execution environment and renewed five minutes before they
expire. Nothing here logs a token, a JWT or a key.
"""

import json
import re
import time
from collections.abc import Callable, Mapping, MutableMapping
from datetime import UTC, datetime
from typing import Any

from mergeguard.config import ConfigError
from mergeguard.github_client import (
    API_URL,
    API_VERSION,
    TIMEOUT_SECONDS,
    USER_AGENT,
    GitHubError,
    classify_status,
)
from mergeguard.http import HttpResponse, Transport, TransportError
from mergeguard.rsa_jwt import PrivateKeyError, RsaKey, make_jwt, parse_private_key

RENEW_BEFORE_SECONDS = 300
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_EXPIRY_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def installation_permissions(*, statuses: bool, contents: bool) -> dict[str, str]:
    """What MergeGuard asks of an installation token. Anything the App was not
    granted makes GitHub refuse the token, so the list stays minimal."""
    permissions = {"pull_requests": "write"}
    if contents:  # reading .github/mergeguard.json (FR-022)
        permissions["contents"] = "read"
    if statuses:  # the mergeguard/review commit status (FR-017)
        permissions["statuses"] = "write"
    return permissions


class AppAuth:
    def __init__(
        self,
        app_id: str,
        private_key_pem: Callable[[], str],
        transport: Transport,
        permissions: Mapping[str, str],
        *,
        cache: MutableMapping[Any, Any] | None = None,
        clock: Callable[[], float] = time.time,
        api_url: str = API_URL,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._transport = transport
        self._permissions = dict(permissions)
        self._cache = cache if cache is not None else {}
        self._clock = clock
        self._api_url = api_url
        self._timeout = timeout
        self._pem: str | None = None
        self._key: RsaKey | None = None

    def installation_token(self, repo: str, installation_id: int) -> str:
        """A token for ``repo`` through installation ``installation_id``."""
        permissions = tuple(sorted(self._permissions.items()))
        cache_key = ("token", installation_id, repo, permissions)
        cached = self._cache.get(cache_key)
        if cached and cached[1] - self._clock() > RENEW_BEFORE_SECONDS:
            return cached[0]
        name = repo.split("/")[1]
        url = f"{self._api_url}/app/installations/{int(installation_id)}/access_tokens"
        payload = {"repositories": [name], "permissions": self._permissions}
        data = _object(self._request("POST", url, payload))
        token, expires = data.get("token"), data.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expires, str):
            raise GitHubError("unexpected", 201)
        try:
            expiry = datetime.strptime(expires, _EXPIRY_FORMAT).replace(tzinfo=UTC)
        except ValueError as exc:
            raise GitHubError("unexpected", 201) from exc
        self._cache[cache_key] = (token, expiry.timestamp())
        return token

    def bot_login(self) -> str:
        """The login of the App's bot account (``<slug>[bot]``), which is the
        author of its reviews. An installation token cannot call ``GET /user``,
        so the duplicate and dismissal checks (TM-14) use the App's slug."""
        cache_key = ("login", self._app_id)
        cached = self._cache.get(cache_key)
        if cached:
            return cached
        slug = _object(self._request("GET", f"{self._api_url}/app")).get("slug")
        if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
            raise GitHubError("unexpected", 200)
        login = f"{slug}[bot]"
        self._cache[cache_key] = login
        return login

    def _jwt(self) -> str:
        pem = self._private_key_pem()
        if pem != self._pem or self._key is None:  # a rotated key is picked up
            try:
                self._key = parse_private_key(pem)
            except PrivateKeyError as exc:
                raise ConfigError("the GitHub App private key is not usable") from exc
            self._pem = pem
        return make_jwt(self._key, self._app_id, int(self._clock()))

    def _request(
        self, method: str, url: str, payload: Mapping[str, Any] | None = None
    ) -> HttpResponse:
        headers = {
            "Authorization": f"Bearer {self._jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        try:
            response = self._transport(method, url, headers, body, self._timeout)
        except TransportError as exc:
            raise GitHubError("unavailable", None) from exc
        kind = classify_status(response)
        if kind is not None:
            raise GitHubError(kind, response.status)
        return response


def _object(response: HttpResponse) -> Mapping[str, Any]:
    try:
        data = json.loads(response.body)
    except ValueError as exc:
        raise GitHubError("unexpected", response.status) from exc
    if not isinstance(data, dict):
        raise GitHubError("unexpected", response.status)
    return data
