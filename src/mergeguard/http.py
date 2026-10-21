"""Minimal HTTPS transport. All network access goes through here, always with an
explicit timeout (AGENTS.md section 5)."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # lower-case header names
    body: bytes


class TransportError(Exception):
    """No HTTP response was received (DNS, TLS, connection or timeout failure)."""


class Transport(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...


def urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResponse:
    if not url.startswith("https://"):
        raise ValueError("only https:// URLs are allowed")
    request = Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310 - https enforced above
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - https enforced above
            return HttpResponse(
                response.status, _lower(response.headers.items()), response.read()
            )
    except HTTPError as exc:
        headers_items = exc.headers.items() if exc.headers is not None else []
        return HttpResponse(exc.code, _lower(headers_items), exc.read())
    except OSError as exc:
        # Only the exception type: messages can contain hostnames or URLs.
        raise TransportError(type(exc).__name__) from exc


def _lower(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {name.lower(): value for name, value in items}
