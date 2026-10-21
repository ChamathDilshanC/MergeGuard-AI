"""Shared test doubles and helpers (docs/TESTING_STRATEGY.md section 3).

Fake credentials follow the fake-secret policy in section 4: AWS uses only the
documented example key ID; anything token-shaped is built at runtime.
"""

import base64
import copy
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mergeguard.http import HttpResponse

FIXTURES = Path(__file__).parent / "fixtures"

AWS_EXAMPLE_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
TEST_WEBHOOK_SECRET = "test-" + "webhook-secret"
TEST_GITHUB_TOKEN = "test-" + "github-token"
BOT_LOGIN = "mergeguard-bot"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


def fake_github_token(prefix: str = "ghp") -> str:
    return prefix + "_" + "x" * 36


def fake_google_api_key() -> str:
    return "AI" + "za" + "Sy" + "A" * 33


def pem_header(key_type: str = "RSA ") -> str:
    return "-----BEGIN " + key_type + "PRIVATE " + "KEY-----"


def load_fixture(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def pull_request_payload(**overrides: Any) -> dict[str, Any]:
    """The opened-PR fixture with dotted-path overrides, e.g. action="closed"."""
    payload = copy.deepcopy(load_fixture("webhooks/pull_request_opened.json"))
    for path, value in overrides.items():
        target = payload
        *parents, leaf = path.split("__")
        for key in parents:
            target = target[key]
        target[leaf] = value
    return payload


def sign(body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def api_event(
    payload: Any,
    *,
    event: str = "pull_request",
    signature: str | None = "auto",
    base64_body: bool = False,
    delivery_id: str = "72d3162e-cc78-11e3-81ab-4c9367dc0958",
) -> dict[str, Any]:
    """An API Gateway payload v2.0 event as Lambda receives it."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {
        "content-type": "application/json",
        "x-github-event": event,
        "x-github-delivery": delivery_id,
    }
    if signature == "auto":
        headers["x-hub-signature-256"] = sign(body)
    elif signature is not None:
        headers["x-hub-signature-256"] = signature
    return {
        "headers": headers,
        "body": base64.b64encode(body).decode() if base64_body else body.decode(),
        "isBase64Encoded": base64_body,
    }


def json_response(
    status: int, data: Any, headers: dict[str, str] | None = None
) -> HttpResponse:
    return HttpResponse(status, headers or {}, json.dumps(data).encode())


def file_entry(filename: str, patch: str | None = "@@ -0,0 +1 @@\n+x = 1") -> dict:
    entry: dict[str, Any] = {"filename": filename, "status": "modified"}
    if patch is not None:
        entry["patch"] = patch
    return entry


@dataclass
class Call:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout: float

    def json(self) -> Any:
        return json.loads(self.body or b"null")


class FakeTransport:
    """Matches requests on method and a URL regex and replays canned responses.

    With several responses for one route they are returned in order and the
    last one repeats. A response may be an exception to raise instead.
    """

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._routes: list[tuple[str, re.Pattern[str], list[Any]]] = []

    def route(self, method: str, pattern: str, *responses: Any) -> "FakeTransport":
        self._routes.append((method, re.compile(pattern), list(responses)))
        return self

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(Call(method, url, dict(headers), body, timeout))
        for route_method, pattern, responses in self._routes:
            if route_method == method and pattern.search(url):
                response = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(response, BaseException):
                    raise response
                return response
        raise AssertionError(f"unexpected request: {method} {url}")

    def requests(self, method: str, pattern: str = "") -> list[Call]:
        return [
            c for c in self.calls if c.method == method and re.search(pattern, c.url)
        ]


class FakeSsm:
    def __init__(
        self, values: dict[str, str] | None = None, error: Exception | None = None
    ):
        self.values = (
            values
            if values is not None
            else {
                "/mergeguard/github_token": TEST_GITHUB_TOKEN,
                "/mergeguard/webhook_secret": TEST_WEBHOOK_SECRET,
            }
        )
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get_parameters(self, *, Names: list[str], WithDecryption: bool) -> dict:
        self.calls.append({"Names": Names, "WithDecryption": WithDecryption})
        if self.error is not None:
            raise self.error
        return {
            "Parameters": [
                {"Name": name, "Value": self.values[name]}
                for name in Names
                if name in self.values
            ],
            "InvalidParameters": [name for name in Names if name not in self.values],
        }


def github_api(
    *,
    files: list[dict] | None = None,
    reviews: list[dict] | None = None,
    review_responses: tuple[Any, ...] = (json_response(200, {"id": 1}),),
) -> FakeTransport:
    """A fake GitHub API serving one page of files, the reviews list, the
    authenticated user and review creation."""
    return (
        FakeTransport()
        .route("GET", r"/pulls/\d+/files", json_response(200, files or []))
        .route("GET", r"/pulls/\d+/reviews", json_response(200, reviews or []))
        .route("GET", r"/user$", json_response(200, {"login": BOT_LOGIN}))
        .route("POST", r"/pulls/\d+/reviews$", *review_responses)
    )
