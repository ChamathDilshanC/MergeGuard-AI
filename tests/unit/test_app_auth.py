"""GitHub App installation tokens (FR-020, T-601, ADR-0010)."""

import base64
import json

import pytest

import rsa_support
from mergeguard.app_auth import AppAuth, installation_permissions
from mergeguard.config import ConfigError
from mergeguard.github_client import GitHubError
from mergeguard.http import TransportError
from mergeguard.rsa_jwt import MIN_KEY_BITS, parse_private_key, verify_rs256
from support import FakeTransport, json_response

TOKENS = r"/app/installations/42/access_tokens$"
APP = r"https://api\.github\.com/app$"
PEM = rsa_support.pkcs8_pem(MIN_KEY_BITS)
KEY = parse_private_key(PEM)
PERMISSIONS = {"pull_requests": "write", "contents": "read", "statuses": "write"}
NOW = 1_700_000_000.0


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def expires_in(seconds: int, now: float = NOW) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(now + seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_response(value: str = "ghs_first", seconds: int = 3600):
    return json_response(201, {"token": value, "expires_at": expires_in(seconds)})


def auth(transport, *, clock=None, pem=PEM, cache=None, permissions=PERMISSIONS):
    return AppAuth(
        "123456",
        lambda: pem,
        transport,
        permissions,
        clock=clock or Clock(),
        cache=cache,
    )


def b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


# --- the token request -----------------------------------------------------------


def test_the_token_request_is_scoped_to_one_repository_and_minimal_permissions():
    transport = FakeTransport().route("POST", TOKENS, token_response())
    token = auth(transport).installation_token("owner/repo", 42)
    assert token == "ghs_first"
    [call] = transport.calls
    assert call.url == "https://api.github.com/app/installations/42/access_tokens"
    assert call.json() == {"repositories": ["repo"], "permissions": PERMISSIONS}
    assert call.headers["Accept"] == "application/vnd.github+json"
    assert call.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert call.timeout == 10.0


def test_the_request_is_signed_with_a_valid_short_lived_app_jwt():
    transport = FakeTransport().route("POST", TOKENS, token_response())
    auth(transport).installation_token("owner/repo", 42)
    scheme, jwt = transport.calls[0].headers["Authorization"].split(" ")
    header, claims, signature = jwt.split(".")
    assert scheme == "Bearer"
    assert json.loads(b64d(header)) == {"alg": "RS256", "typ": "JWT"}
    parsed = json.loads(b64d(claims))
    assert parsed == {"iat": NOW - 60, "exp": NOW + 540, "iss": "123456"}
    assert verify_rs256(KEY, f"{header}.{claims}".encode(), b64d(signature))


def test_permissions_follow_the_features_that_are_switched_on():
    assert installation_permissions(statuses=False, contents=False) == {
        "pull_requests": "write"
    }
    assert installation_permissions(statuses=True, contents=False) == {
        "pull_requests": "write",
        "statuses": "write",
    }
    assert installation_permissions(statuses=False, contents=True) == {
        "pull_requests": "write",
        "contents": "read",
    }
    assert installation_permissions(statuses=True, contents=True) == PERMISSIONS


# --- caching ---------------------------------------------------------------------


def test_a_token_is_reused_until_shortly_before_it_expires():
    transport = FakeTransport().route(
        "POST", TOKENS, token_response("ghs_first"), token_response("ghs_second")
    )
    clock = Clock()
    app = auth(transport, clock=clock)
    assert app.installation_token("owner/repo", 42) == "ghs_first"
    clock.now += 3000  # 10 minutes left
    assert app.installation_token("owner/repo", 42) == "ghs_first"
    assert len(transport.calls) == 1
    clock.now += 400  # under five minutes left: renew
    assert app.installation_token("owner/repo", 42) == "ghs_second"
    assert len(transport.calls) == 2


def test_each_repository_and_installation_gets_its_own_token():
    transport = (
        FakeTransport()
        .route("POST", TOKENS, token_response("ghs_a"), token_response("ghs_b"))
        .route("POST", r"/installations/43/", token_response("ghs_c"))
    )
    app = auth(transport)
    assert app.installation_token("owner/repo-a", 42) == "ghs_a"
    assert app.installation_token("owner/repo-b", 42) == "ghs_b"
    assert app.installation_token("other/repo-a", 43) == "ghs_c"
    assert [c.json()["repositories"] for c in transport.calls] == [
        ["repo-a"],
        ["repo-b"],
        ["repo-a"],
    ]


def test_a_shared_cache_survives_new_auth_objects():
    transport = FakeTransport().route("POST", TOKENS, token_response())
    cache: dict = {}
    auth(transport, cache=cache).installation_token("owner/repo", 42)
    auth(transport, cache=cache).installation_token("owner/repo", 42)
    assert len(transport.calls) == 1


def test_a_cached_token_is_not_shared_across_permission_sets():
    transport = FakeTransport().route(
        "POST", TOKENS, token_response("ghs_wide"), token_response("ghs_narrow")
    )
    cache: dict = {}
    auth(transport, cache=cache).installation_token("owner/repo", 42)
    narrow = auth(transport, cache=cache, permissions={"pull_requests": "write"})
    assert narrow.installation_token("owner/repo", 42) == "ghs_narrow"


# --- errors ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (json_response(401, {}), "auth_failed"),
        (json_response(403, {}), "forbidden"),
        (json_response(404, {}), "forbidden"),  # App not installed on the repository
        (json_response(422, {}), "invalid_request"),  # permissions not granted
        (json_response(500, {}), "unavailable"),
        (TransportError("timeout"), "unavailable"),
    ],
)
def test_token_errors_map_to_the_github_error_kinds(response, kind):
    transport = FakeTransport().route("POST", TOKENS, response)
    with pytest.raises(GitHubError) as raised:
        auth(transport).installation_token("owner/repo", 42)
    assert raised.value.kind == kind


@pytest.mark.parametrize(
    "response",
    [
        json_response(201, []),
        json_response(201, {"expires_at": expires_in(3600)}),
        json_response(201, {"token": "", "expires_at": expires_in(3600)}),
        json_response(201, {"token": "ghs_x"}),
        json_response(201, {"token": "ghs_x", "expires_at": "tomorrow"}),
    ],
)
def test_unusable_token_responses_are_unexpected(response):
    transport = FakeTransport().route("POST", TOKENS, response)
    with pytest.raises(GitHubError) as raised:
        auth(transport).installation_token("owner/repo", 42)
    assert raised.value.kind == "unexpected"


def test_a_failed_request_is_not_cached():
    transport = FakeTransport().route(
        "POST", TOKENS, json_response(500, {}), token_response()
    )
    app = auth(transport)
    with pytest.raises(GitHubError):
        app.installation_token("owner/repo", 42)
    assert app.installation_token("owner/repo", 42) == "ghs_first"


def test_a_non_json_body_is_unexpected():
    from mergeguard.http import HttpResponse

    transport = FakeTransport().route("POST", TOKENS, HttpResponse(201, {}, b"<html>"))
    with pytest.raises(GitHubError) as raised:
        auth(transport).installation_token("owner/repo", 42)
    assert raised.value.kind == "unexpected"


# --- the bot login ---------------------------------------------------------------


def test_the_bot_login_comes_from_the_app_slug_and_is_cached():
    transport = FakeTransport().route(
        "GET", APP, json_response(200, {"slug": "mergeguard-ai"})
    )
    app = auth(transport)
    assert app.bot_login() == "mergeguard-ai[bot]"
    assert app.bot_login() == "mergeguard-ai[bot]"
    assert len(transport.calls) == 1
    assert (
        "Bearer " in transport.calls[0].headers["Authorization"]
    )  # the JWT, not a token


@pytest.mark.parametrize(
    "data", [[], {}, {"slug": ""}, {"slug": "bad slug"}, {"slug": 7}]
)
def test_an_unusable_slug_is_unexpected(data):
    transport = FakeTransport().route("GET", APP, json_response(200, data))
    with pytest.raises(GitHubError) as raised:
        auth(transport).bot_login()
    assert raised.value.kind == "unexpected"


# --- the private key -------------------------------------------------------------


def test_an_unusable_key_fails_closed_without_echoing_it():
    transport = FakeTransport()
    with pytest.raises(ConfigError) as raised:
        auth(transport, pem="not a key").installation_token("owner/repo", 42)
    assert "not a key" not in str(raised.value)
    assert transport.calls == []  # nothing is sent without a usable key


def test_a_key_below_the_size_floor_fails_closed():
    weak = rsa_support.pkcs1_pem(1024)
    with pytest.raises(ConfigError):
        auth(FakeTransport(), pem=weak).installation_token("owner/repo", 42)


def test_a_rotated_key_is_picked_up_without_a_restart():
    transport = FakeTransport().route(
        "POST", TOKENS, token_response("ghs_a"), token_response("ghs_b")
    )
    keys = [PEM, rsa_support.pkcs1_pem(MIN_KEY_BITS, seed=7)]
    current = {"pem": keys[0]}
    app = AppAuth(
        "123456",
        lambda: current["pem"],
        transport,
        PERMISSIONS,
        clock=Clock(),
        cache={},
    )
    app.installation_token("owner/repo-a", 42)
    current["pem"] = keys[1]
    app.installation_token("owner/repo-b", 42)
    first, second = (c.headers["Authorization"].split(" ")[1] for c in transport.calls)
    new_key = parse_private_key(keys[1])
    head, claims, signature = second.split(".")
    assert verify_rs256(new_key, f"{head}.{claims}".encode(), b64d(signature))
    assert not verify_rs256(
        new_key, ".".join(first.split(".")[:2]).encode(), b64d(first.split(".")[2])
    )
