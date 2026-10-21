"""Minimal HTTPS transport. All network access goes through here, always with an
explicit timeout (AGENTS.md section 5)."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # lower-case header names
    body: bytes


class TransportError(Exception):
    """No HTTP response was received. ``kind`` is ``"timeout"`` or ``"network"``
    (DNS, TLS or connection failure)."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


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
        # Only a category: messages can contain hostnames or URLs.
        raise TransportError("timeout" if _is_timeout(exc) else "network") from exc


def _is_timeout(exc: OSError) -> bool:
    # urlopen raises TimeoutError on read timeouts but wraps connect timeouts.
    return isinstance(exc, TimeoutError) or (
        isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)
    )


def _lower(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {name.lower(): value for name, value in items}
