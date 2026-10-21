"""The urllib transport. ``urlopen`` is replaced only inside mergeguard.http,
never globally (docs/TESTING_STRATEGY.md section 3)."""

import io
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from mergeguard import http
from mergeguard.http import TransportError, urllib_transport


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for name, value in headers.items():
            self.headers[name] = value


def test_success_returns_status_lowercased_headers_and_body(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, method=request.get_method(), timeout=timeout)
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(200, b"[]", {"X-RateLimit-Remaining": "59"})

    monkeypatch.setattr(http, "urlopen", fake_urlopen)
    response = urllib_transport(
        "GET", "https://api.github.com/user", {"Authorization": "Bearer t"}, None, 10.0
    )
    assert (response.status, response.body) == (200, b"[]")
    assert response.headers == {"x-ratelimit-remaining": "59"}
    assert seen == {
        "url": "https://api.github.com/user",
        "method": "GET",
        "timeout": 10.0,
        "auth": "Bearer t",
    }


def test_http_errors_become_responses(monkeypatch):
    headers = Message()
    headers["Retry-After"] = "30"

    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 429, "Too Many", headers, io.BytesIO(b"{}"))

    monkeypatch.setattr(http, "urlopen", fake_urlopen)
    response = urllib_transport("GET", "https://api.github.com/x", {}, None, 5.0)
    assert (response.status, response.headers, response.body) == (
        429,
        {"retry-after": "30"},
        b"{}",
    )


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (TimeoutError(), "timeout"),  # read timeout
        (URLError(TimeoutError("timed out")), "timeout"),  # connect timeout
        (URLError("dns failure"), "network"),
        (ConnectionResetError(), "network"),
    ],
)
def test_network_failures_raise_transport_error_without_details(
    monkeypatch, error, kind
):
    def fake_urlopen(request, timeout):
        raise error

    monkeypatch.setattr(http, "urlopen", fake_urlopen)
    with pytest.raises(TransportError) as caught:
        urllib_transport("GET", "https://api.github.com/secret-path", {}, None, 5.0)
    assert caught.value.kind == kind
    assert "secret-path" not in str(caught.value)


def test_only_https_is_allowed():
    with pytest.raises(ValueError):
        urllib_transport("GET", "http://api.github.com/user", {}, None, 5.0)
    with pytest.raises(ValueError):
        urllib_transport("GET", "file:///etc/passwd", {}, None, 5.0)
