"""Gemini AI review (FR-007, FR-008; docs/AI_SYSTEM.md).

Called only after a clean secret scan (ADR-0004). The diff is untrusted data:
it travels only inside per-request random boundary markers in the user content,
never in the system instruction, and the model gets no tools. Every failure
becomes a reason category for the AI-unavailable comment; nothing in this
module can approve or block a pull request.
"""

import json
import re
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from mergeguard.config import ConfigError
from mergeguard.github_client import USER_AGENT
from mergeguard.http import HttpResponse, Transport, TransportError
from mergeguard.models import AIReview, ChangedFile, DiffBundle
from mergeguard.prompts import (
    RESPONSE_SCHEMA,
    SYSTEM_INSTRUCTION,
    format_file,
    user_content,
)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
TEMPERATURE = 0.2
MAX_TIMEOUT_SECONDS = 20.0
MIN_TIMEOUT_SECONDS = 3.0
# Time kept for posting the review after the Gemini call (AI_SYSTEM.md 6).
POSTING_RESERVE_SECONDS = 5.0
RISK_LEVELS = frozenset({"LOW", "MEDIUM", "HIGH"})
MAX_BUGS = 1000
MAX_BOUNDARY_ATTEMPTS = 10

_HUNK_START_RE = re.compile(r"(?m)^(?=@@ )")


class AIReviewFailure(Exception):
    """The AI review could not be used; ``reason`` is the T-AIU category."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AIOutcome:
    review: AIReview | None
    failure: str | None  # reason category when review is None
    bundle: DiffBundle
    duration_ms: int


def _no_deadline() -> float | None:
    return None


def _random_boundary() -> str:
    return secrets.token_hex(16)


class GeminiReviewer:
    def __init__(
        self,
        api_key: Callable[[], str],
        transport: Transport,
        *,
        model: str,
        max_diff_chars: int,
        time_left: Callable[[], float | None] = _no_deadline,
        boundary: Callable[[], str] = _random_boundary,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self.model = model
        self._max_diff_chars = max_diff_chars
        self._time_left = time_left
        self._boundary = boundary
        self._clock = clock

    def review(self, files: Sequence[ChangedFile]) -> AIOutcome:
        started = self._clock()
        bundle = build_diff_bundle(files, self._max_diff_chars)
        try:
            review, failure = self._call(bundle), None
        except AIReviewFailure as exc:
            review, failure = None, exc.reason
        duration_ms = round((self._clock() - started) * 1000)
        return AIOutcome(review, failure, bundle, duration_ms)

    def _call(self, bundle: DiffBundle) -> AIReview:
        if not bundle.text:  # not even part of the first file fits the budget
            raise AIReviewFailure("diff_too_large")
        timeout = self._timeout()
        try:
            key = self._api_key()  # read from SSM only now, when it is needed
        except ConfigError as exc:
            raise AIReviewFailure("not_configured") from exc
        payload = build_request(bundle.text, self._unique_boundary(bundle.text))
        url = GEMINI_URL.format(model=quote(self.model, safe=""))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "x-goog-api-key": key,  # never in the URL (RB-07)
        }
        try:
            response = self._transport(
                "POST", url, headers, json.dumps(payload).encode("utf-8"), timeout
            )
        except TransportError as exc:
            reason = "timeout" if exc.kind == "timeout" else "unavailable"
            raise AIReviewFailure(reason) from exc
        _raise_for_status(response)
        return parse_review(response.body, self.model)

    def _timeout(self) -> float:
        """Derive the timeout from the Lambda deadline, keeping time to post."""
        remaining = self._time_left()
        if remaining is None:
            return MAX_TIMEOUT_SECONDS
        budget = min(MAX_TIMEOUT_SECONDS, remaining - POSTING_RESERVE_SECONDS)
        if budget < MIN_TIMEOUT_SECONDS:
            raise AIReviewFailure("timeout")
        return budget

    def _unique_boundary(self, text: str) -> str:
        # A diff that already contains the marker could close the block early.
        for _ in range(MAX_BOUNDARY_ATTEMPTS):
            boundary = self._boundary()
            if boundary and boundary not in text:
                return boundary
        raise AIReviewFailure("unavailable")


def build_request(diff_text: str, boundary: str) -> dict[str, Any]:
    """The generateContent body (docs/API_SPEC.md section 4). No tools."""
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [
            {"role": "user", "parts": [{"text": user_content(diff_text, boundary)}]}
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": TEMPERATURE,
        },
    }


def build_diff_bundle(files: Sequence[ChangedFile], max_chars: int) -> DiffBundle:
    """Add whole files in API order until the next one would exceed the budget.
    If even the first file is too big, send its first hunks (AI_SYSTEM.md 6)."""
    sections: list[str] = []
    included: list[str] = []
    omitted: list[str] = []
    used = 0
    for changed in files:
        if not changed.patch:
            continue
        if omitted:  # budget exhausted: everything after is omitted, in order
            omitted.append(changed.filename)
            continue
        section = format_file(changed.filename, changed.patch)
        separator = 2 if sections else 0
        if used + separator + len(section) <= max_chars:
            sections.append(section)
            included.append(changed.filename)
            used += separator + len(section)
            continue
        if not sections:
            partial = _partial_section(changed.filename, changed.patch, max_chars)
            if partial:
                sections.append(partial)
        omitted.append(changed.filename)
    return DiffBundle(
        text="\n\n".join(sections),
        included_files=tuple(included),
        omitted_files=tuple(omitted),
        truncated=bool(omitted),
        max_chars=max_chars,
    )


def _partial_section(filename: str, patch: str, max_chars: int) -> str:
    # The fence for the full patch is at least as long as for any prefix of it.
    budget = max_chars - (len(format_file(filename, patch)) - len(patch))
    kept = ""
    for hunk in filter(None, _HUNK_START_RE.split(patch)):
        if len(kept) + len(hunk) > budget:
            break
        kept += hunk
    if not kept:  # even the first hunk is too big: cut it at a line boundary
        for line in patch.split("\n"):
            if len(kept) + len(line) + 1 > budget:
                break
            kept += line + "\n"
    kept = kept.rstrip("\n")
    has_code = any(
        line.startswith(("+", "-", " ")) for line in kept.split("\n")
    )  # a bare hunk header is not worth sending
    return format_file(filename, kept) if has_code else ""


def parse_review(body: bytes, model: str) -> AIReview:
    """Extract and strictly validate the review (AI_SYSTEM.md sections 5, 8)."""
    data = _loads(body)
    if not isinstance(data, dict):
        raise AIReviewFailure("invalid_output")
    feedback = data.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise AIReviewFailure("blocked")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise AIReviewFailure("blocked")
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise AIReviewFailure("invalid_output")
    if candidate.get("finishReason") != "STOP":
        raise AIReviewFailure("incomplete")
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise AIReviewFailure("invalid_output")
    text = "".join(
        part["text"]
        for part in parts
        if isinstance(part, dict)
        and isinstance(part.get("text"), str)
        and not part.get("thought")
    )
    return validate_review(_loads(text), model)


def validate_review(value: object, model: str) -> AIReview:
    if not isinstance(value, dict):
        raise AIReviewFailure("invalid_output")
    risk = value.get("risk_level")
    bugs = value.get("bugs_found")
    summary = value.get("summary")
    feedback = value.get("feedback_markdown")
    valid = (
        isinstance(risk, str)
        and risk.strip() in RISK_LEVELS
        and type(bugs) is int
        and 0 <= bugs <= MAX_BUGS
        and isinstance(summary, str)
        and isinstance(feedback, str)
    )
    if not valid:
        raise AIReviewFailure("invalid_output")
    return AIReview(risk.strip(), bugs, summary, feedback, model)


def _raise_for_status(response: HttpResponse) -> None:
    status = response.status
    if 200 <= status < 300:
        return
    if status in (401, 403):
        raise AIReviewFailure("auth")
    if status == 429:
        raise AIReviewFailure("rate_limited")
    if status >= 500:
        raise AIReviewFailure("unavailable")
    raise AIReviewFailure("bad_request")  # 400, or 404 for an unknown model


def _loads(raw: bytes | str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise AIReviewFailure("invalid_output") from exc
