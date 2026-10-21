"""Webhook boundary: body decoding, signature verification and event triage.

Implements FR-002 and FR-003. The signature is always verified before the body
is parsed (docs/SECURITY.md section 5.1).
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mergeguard.models import PullRequestRef

SIGNATURE_PREFIX = "sha256="
REVIEWABLE_ACTIONS = frozenset(
    {"opened", "synchronize", "reopened", "ready_for_review"}
)
# Proposed default; whether to review drafts is open question OQ-09.
SKIP_DRAFTS = True

_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class InvalidBody(ValueError):
    """The request body could not be decoded, so it cannot be authenticated."""


class MalformedPayload(ValueError):
    """The body is authentic but not a usable event payload."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Triage:
    """What to do with an authenticated delivery."""

    status: str  # "pong", "ignored" or "review"
    reason: str | None = None
    action: str | None = None
    pull_request: PullRequestRef | None = None


def lower_headers(event: Mapping[str, Any]) -> dict[str, str]:
    headers = event.get("headers") or {}
    return {str(name).lower(): str(value) for name, value in headers.items()}


def raw_body(event: Mapping[str, Any]) -> bytes:
    """Return the exact bytes GitHub signed (base64-decoded when flagged)."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidBody("body is not valid base64") from exc
    return body.encode("utf-8")


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check ``X-Hub-Signature-256`` in constant time. Fails closed on an empty
    secret."""
    if not secret:
        raise ValueError("webhook secret is empty")
    if not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = (SIGNATURE_PREFIX + digest).encode("ascii")
    return hmac.compare_digest(expected, header.encode("utf-8"))


def triage(event_name: str, body: bytes) -> Triage:
    """Classify an authenticated delivery (FR-003)."""
    if event_name == "ping":
        return Triage("pong")
    if event_name != "pull_request":
        return Triage("ignored", "event_not_supported")
    payload = _parse_object(body)
    action = payload.get("action")
    if not isinstance(action, str) or action not in REVIEWABLE_ACTIONS:
        return Triage("ignored", "action_not_supported")
    pull_request = parse_pull_request(payload)
    if SKIP_DRAFTS and pull_request.draft:
        return Triage("ignored", "draft", action, pull_request)
    return Triage("review", None, action, pull_request)


def parse_pull_request(payload: Mapping[str, Any]) -> PullRequestRef:
    pr = _object(payload.get("pull_request"))
    repo = _object(payload.get("repository")).get("full_name")
    number = pr.get("number")
    head_sha = _object(pr.get("head")).get("sha")
    login = _object(pr.get("user")).get("login")
    draft = pr.get("draft", False)
    valid = (
        is_valid_repo(repo)
        and is_valid_pull_number(number)
        and is_valid_sha(head_sha)
        and isinstance(draft, bool)
    )
    if not valid:
        raise MalformedPayload("malformed_payload")
    return PullRequestRef(
        repo=repo,
        number=number,
        head_sha=head_sha,
        author_login=login if isinstance(login, str) else "",
        draft=draft,
        installation_id=_installation_id(payload),
    )


def is_valid_repo(value: object) -> bool:
    """``owner/name`` with no path tricks; it becomes part of API URLs."""
    return (
        isinstance(value, str)
        and _REPO_RE.fullmatch(value) is not None
        and not any(part in (".", "..") for part in value.split("/"))
    )


def is_valid_pull_number(value: object) -> bool:
    return type(value) is int and value > 0


def is_valid_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def _installation_id(payload: Mapping[str, Any]) -> int | None:
    """``installation.id`` of an App webhook; absent for a plain repository
    webhook. A present but unusable value is a malformed payload."""
    if "installation" not in payload:
        return None
    installation_id = _object(payload["installation"]).get("id")
    if not is_valid_pull_number(installation_id):
        raise MalformedPayload("malformed_payload")
    return installation_id


def _parse_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise MalformedPayload("malformed_json") from exc
    return _object(payload)


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MalformedPayload("malformed_payload")
    return value
