"""GitHub client contract tests against a fake transport (FR-004, API_SPEC 3.2)."""

import pytest

from mergeguard.github_client import GitHubClient, GitHubError, classify_status
from mergeguard.http import HttpResponse, TransportError
from support import TEST_GITHUB_TOKEN, FakeTransport, file_entry, json_response

FILES = r"/repos/owner/repo/pulls/7/files"


def client(transport: FakeTransport, **kwargs) -> tuple[GitHubClient, list[float]]:
    sleeps: list[float] = []
    kwargs.setdefault("sleep", sleeps.append)
    return GitHubClient(TEST_GITHUB_TOKEN, transport, **kwargs), sleeps


def page(start: int, count: int) -> list[dict]:
    return [file_entry(f"f{i}.py") for i in range(start, start + count)]


def test_requests_carry_the_documented_headers_and_timeout():
    transport = FakeTransport().route("GET", FILES, json_response(200, []))
    client(transport)[0].list_pull_files("owner/repo", 7, 300)
    [call] = transport.calls
    assert call.headers == {
        "Authorization": f"Bearer {TEST_GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MergeGuard-AI-Bot",
    }
    assert call.timeout == 10.0


def test_files_are_paginated_until_a_short_page():
    transport = FakeTransport().route(
        "GET", FILES, json_response(200, page(0, 100)), json_response(200, page(100, 5))
    )
    files, capped = client(transport)[0].list_pull_files("owner/repo", 7, 300)
    assert len(files) == 105 and capped is False
    assert [c.url.split("?")[1] for c in transport.calls] == [
        "per_page=100&page=1",
        "per_page=100&page=2",
    ]


def test_file_cap_stops_paging_and_reports_truncation():
    transport = FakeTransport().route("GET", FILES, json_response(200, page(0, 100)))
    files, capped = client(transport)[0].list_pull_files("owner/repo", 7, 150)
    assert len(files) == 150 and capped is True
    assert len(transport.calls) == 2


def test_exactly_max_files_on_a_short_page_is_not_truncated():
    transport = FakeTransport().route("GET", FILES, json_response(200, page(0, 50)))
    files, capped = client(transport)[0].list_pull_files("owner/repo", 7, 50)
    assert len(files) == 50 and capped is False


def test_files_without_patch_are_kept_with_patch_none():
    transport = FakeTransport().route(
        "GET", FILES, json_response(200, [file_entry("logo.png", patch=None)])
    )
    [changed] = client(transport)[0].list_pull_files("owner/repo", 7, 300)[0]
    assert (changed.filename, changed.patch) == ("logo.png", None)


@pytest.mark.parametrize("body", [[{"status": "added"}], ["oops"], {"not": "a list"}])
def test_unexpected_payloads_raise(body):
    transport = FakeTransport().route("GET", FILES, json_response(200, body))
    with pytest.raises(GitHubError) as caught:
        client(transport)[0].list_pull_files("owner/repo", 7, 300)
    assert caught.value.kind == "unexpected"


def test_non_json_body_raises_unexpected():
    transport = FakeTransport().route("GET", FILES, HttpResponse(200, {}, b"<html>"))
    with pytest.raises(GitHubError) as caught:
        client(transport)[0].list_pull_files("owner/repo", 7, 300)
    assert caught.value.kind == "unexpected"


@pytest.mark.parametrize(
    ("status", "headers", "kind"),
    [
        (401, {}, "auth_failed"),
        (403, {"x-ratelimit-remaining": "0"}, "rate_limited"),
        (403, {"retry-after": "60"}, "rate_limited"),
        (429, {}, "rate_limited"),
        (403, {}, "forbidden"),
        (404, {}, "forbidden"),
        (422, {}, "invalid_request"),
        (500, {}, "unavailable"),
        (503, {}, "unavailable"),
        (418, {}, "unexpected"),
        (204, {}, None),
    ],
)
def test_status_classification_matches_api_spec(status, headers, kind):
    assert classify_status(HttpResponse(status, headers, b"")) == kind


@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_non_retryable_errors_fail_after_one_call(status):
    transport = FakeTransport().route("GET", FILES, json_response(status, {}))
    gh, sleeps = client(transport)
    with pytest.raises(GitHubError) as caught:
        gh.list_pull_files("owner/repo", 7, 300)
    assert caught.value.status == status
    assert len(transport.calls) == 1 and sleeps == []


@pytest.mark.parametrize(
    "first",
    [
        json_response(500, {}),
        json_response(429, {}),
        json_response(403, {}, {"x-ratelimit-remaining": "0"}),
        TransportError("TimeoutError"),
    ],
)
def test_get_is_retried_once_after_a_short_delay(first):
    transport = FakeTransport().route("GET", FILES, first, json_response(200, []))
    gh, sleeps = client(transport)
    assert gh.list_pull_files("owner/repo", 7, 300) == ([], False)
    assert len(transport.calls) == 2 and sleeps == [1.0]


def test_second_failure_is_raised():
    transport = FakeTransport().route("GET", FILES, json_response(503, {}))
    with pytest.raises(GitHubError) as caught:
        client(transport)[0].list_pull_files("owner/repo", 7, 300)
    assert (caught.value.kind, len(transport.calls)) == ("unavailable", 2)


def test_no_retry_when_the_lambda_is_out_of_time():
    transport = FakeTransport().route("GET", FILES, json_response(500, {}))
    gh, sleeps = client(transport, has_time=lambda seconds: False)
    with pytest.raises(GitHubError):
        gh.list_pull_files("owner/repo", 7, 300)
    assert len(transport.calls) == 1 and sleeps == []


def test_transport_failure_after_retry_has_no_status():
    transport = FakeTransport().route("GET", FILES, TransportError("TimeoutError"))
    with pytest.raises(GitHubError) as caught:
        client(transport)[0].list_pull_files("owner/repo", 7, 300)
    assert (caught.value.kind, caught.value.status) == ("unavailable", None)


def test_post_is_not_retried_on_server_errors():
    """A POST that got a 5xx may still have created the review."""
    transport = FakeTransport().route("POST", r"/reviews$", json_response(502, {}))
    gh, sleeps = client(transport)
    with pytest.raises(GitHubError):
        gh.create_review("owner/repo", 7, commit_id="a" * 40, event="COMMENT", body="b")
    assert len(transport.calls) == 1 and sleeps == []


def test_post_is_retried_on_rate_limit():
    transport = FakeTransport().route(
        "POST", r"/reviews$", json_response(429, {}), json_response(200, {})
    )
    client(transport)[0].create_review(
        "owner/repo", 7, commit_id="a" * 40, event="COMMENT", body="b"
    )
    assert len(transport.calls) == 2


def test_create_review_sends_the_documented_body():
    transport = FakeTransport().route(
        "POST", r"/repos/owner/repo/pulls/7/reviews$", json_response(200, {})
    )
    client(transport)[0].create_review(
        "owner/repo", 7, commit_id="a" * 40, event="REQUEST_CHANGES", body="text"
    )
    [call] = transport.calls
    assert call.json() == {
        "commit_id": "a" * 40,
        "event": "REQUEST_CHANGES",
        "body": "text",
    }
    assert call.headers["Content-Type"] == "application/json"


def test_create_review_refuses_approve_without_calling_github():
    transport = FakeTransport()
    with pytest.raises(ValueError):
        client(transport)[0].create_review(
            "owner/repo", 7, commit_id="a" * 40, event="APPROVE", body="lgtm"
        )
    assert transport.calls == []


def test_reviews_are_paginated():
    reviews = [{"id": i} for i in range(100)]
    transport = FakeTransport().route(
        "GET",
        r"/pulls/7/reviews",
        json_response(200, reviews),
        json_response(200, [{"id": 100}, "junk"]),
    )
    assert len(client(transport)[0].list_reviews("owner/repo", 7)) == 101


def test_authenticated_login_is_cached():
    transport = FakeTransport().route(
        "GET", r"/user$", json_response(200, {"login": "bot"})
    )
    gh = client(transport)[0]
    assert gh.authenticated_login() == gh.authenticated_login() == "bot"
    assert len(transport.calls) == 1


def test_authenticated_login_requires_a_login():
    transport = FakeTransport().route("GET", r"/user$", json_response(200, {}))
    with pytest.raises(GitHubError):
        client(transport)[0].authenticated_login()


def test_path_segments_are_url_quoted():
    transport = FakeTransport().route("GET", r"/files", json_response(200, []))
    client(transport)[0].list_pull_files("my.org/repo_name-1", 7, 10)
    assert transport.calls[0].url.startswith(
        "https://api.github.com/repos/my.org/repo_name-1/pulls/7/files?"
    )
