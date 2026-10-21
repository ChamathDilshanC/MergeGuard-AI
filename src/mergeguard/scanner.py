"""Deterministic secret scanner over the added lines of PR patches.

Implements FR-005 and ADR-0004. Fixes two defects of the brief's scanner:
RB-02 (private-key pattern missed RSA, OpenSSH and EC keys) and RB-03 (removed
and context lines were scanned, so removing a secret got the PR blocked).
"""

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from mergeguard.models import ChangedFile, SecretFinding

GENERIC_RULE_ID = "generic-assignment"

_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_FILE_HEADER_PREFIXES = ("+++ ", "--- ", "diff --git ", "index ")


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    pattern: re.Pattern[str]


# Rule IDs are a contract (docs/DESIGN_SYSTEM.md section 8); never rename one.
RULES = (
    Rule(
        "aws-access-key-id",
        re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ),
    # Any key-type words before PRIVATE KEY: RSA, EC, DSA, OPENSSH, ENCRYPTED,
    # PGP ... BLOCK, or none at all (PKCS#8).
    Rule(
        "private-key-pem",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    ),
    Rule(
        "github-token",
        re.compile(
            r"(?<![A-Za-z0-9_])"
            r"(?:(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"
        ),
    ),
    Rule(
        "google-api-key",
        re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"),
    ),
)

# The brief's generic rule (a sensitive name assigned a quoted literal of 8+
# characters), extended to quoted JSON keys, ':' and compound names such as
# DB_PASSWORD, apiKey or SECRET_KEY.
_ASSIGNMENT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<key>[A-Za-z_][A-Za-z0-9_]*)['\"]?\s*[:=]\s*"
    r"(?P<quote>['\"])(?P<value>[^'\"\s]{8,})(?P=quote)"
)
_SENSITIVE_LAST_WORDS = frozenset({"password", "passwd", "secret", "token", "apikey"})
_KEY_QUALIFIERS = frozenset({"api", "secret", "private"})
_PLACEHOLDER_MARKERS = (
    "example",
    "placeholder",
    "changeme",
    "your_",
    "your-",
    "redacted",
    "dummy",
    "<",
    "${",
    "{{",
    "%(",
)
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
_REPEATED_CHAR_RE = re.compile(r"(.)\1*")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")


def added_lines(patch: str) -> Iterator[tuple[int, str]]:
    """Yield (new-file line number, content) for each added line of a patch."""
    new_line = 1
    in_hunk = False
    for raw in patch.split("\n"):
        hunk = _HUNK_RE.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            in_hunk = True
        elif not in_hunk and raw.startswith(_FILE_HEADER_PREFIXES):
            continue  # file headers only appear before the first hunk
        elif raw.startswith("+"):
            yield new_line, raw[1:]
            new_line += 1
        elif raw.startswith(("-", "\\")):
            continue  # removed line, or "\ No newline at end of file"
        else:
            new_line += 1  # context line


def scan_patch(filename: str, patch: str) -> list[SecretFinding]:
    return [
        SecretFinding(rule_id, filename, line)
        for line, text in added_lines(patch)
        for rule_id in matching_rules(text)
    ]


def scan_files(files: Iterable[ChangedFile]) -> list[SecretFinding]:
    return [
        finding
        for changed in files
        if changed.patch
        for finding in scan_patch(changed.filename, changed.patch)
    ]


def matching_rules(text: str) -> list[str]:
    """Rule IDs that match one line. The generic rule only reports lines that no
    specific rule already explains."""
    matched = [rule.rule_id for rule in RULES if rule.pattern.search(text)]
    if not matched and _has_sensitive_assignment(text):
        matched.append(GENERIC_RULE_ID)
    return matched


def _has_sensitive_assignment(text: str) -> bool:
    return any(
        _is_sensitive_name(match["key"]) and not _is_placeholder(match["value"])
        for match in _ASSIGNMENT_RE.finditer(text)
    )


def _is_sensitive_name(name: str) -> bool:
    words = _CAMEL_BOUNDARY_RE.sub(r"\1_\2", name).lower().split("_")
    last = words[-1]
    if last in _SENSITIVE_LAST_WORDS:
        return True
    return last == "key" and any(word in _KEY_QUALIFIERS for word in words[:-1])


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return (
        any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
        or (value.startswith("{") and value.endswith("}"))  # "{name}" template
        or _REPEATED_CHAR_RE.fullmatch(value) is not None
        or _ENV_NAME_RE.fullmatch(value) is not None
    )
