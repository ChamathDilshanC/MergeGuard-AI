"""Deterministic secret scanner over the added lines of PR patches.

Implements FR-005 and ADR-0004. Fixes two defects of the brief's scanner:
RB-02 (private-key pattern missed RSA, OpenSSH and EC keys) and RB-03 (removed
and context lines were scanned, so removing a secret got the PR blocked).
"""

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace

from mergeguard.models import ChangedFile, SecretFinding, SuppressedFinding

GENERIC_RULE_ID = "generic-assignment"
PEM_RULE_ID = "private-key-pem"
# FR-019: a line carrying this text is a declared false positive. Contract:
# changing it needs the same versioning as a rule ID.
INLINE_SUPPRESSION_MARKER = "mergeguard:ignore-secret"
# Stands in for a suppressed line in the diff sent to the AI (ADR-0008).
WITHHELD_LINE = "+[line withheld: suppressed secret finding]"

_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_PEM_END_RE = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
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
        PEM_RULE_ID,
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


def _added_rows(rows: list[str]) -> Iterator[tuple[int, int]]:
    """Yield (row index, new-file line number) for each added row of a patch."""
    new_line = 1
    in_hunk = False
    for index, raw in enumerate(rows):
        hunk = _HUNK_RE.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            in_hunk = True
        elif not in_hunk and raw.startswith(_FILE_HEADER_PREFIXES):
            continue  # file headers only appear before the first hunk
        elif raw.startswith("+"):
            yield index, new_line
            new_line += 1
        elif raw.startswith(("-", "\\")):
            continue  # removed line, or "\ No newline at end of file"
        else:
            new_line += 1  # context line


def added_lines(patch: str) -> Iterator[tuple[int, str]]:
    """Yield (new-file line number, content) for each added line of a patch."""
    rows = patch.split("\n")
    for index, line in _added_rows(rows):
        yield line, rows[index][1:]


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


@dataclass(frozen=True, slots=True)
class ScanResult:
    findings: list[SecretFinding]  # these block the pull request
    suppressed: list[SuppressedFinding]  # listed in the review, never hidden


def scan(files: Iterable[ChangedFile], allow_paths: Sequence[str] = ()) -> ScanResult:
    """Scan added lines and apply the FR-019 suppressions.

    A hit is suppressed when its file matches an operator-configured path
    pattern, or when its own line carries ``INLINE_SUPPRESSION_MARKER``. Nothing
    is dropped silently: the caller lists every suppression in the review.
    """
    patterns = [compile_allow_path(pattern) for pattern in allow_paths]
    findings: list[SecretFinding] = []
    suppressed: list[SuppressedFinding] = []
    for changed in files:
        if not changed.patch:
            continue
        path_allowed = any(pattern.fullmatch(changed.filename) for pattern in patterns)
        for line, text in added_lines(changed.patch):
            for rule_id in matching_rules(text):
                if path_allowed:
                    suppressed.append(
                        SuppressedFinding(rule_id, changed.filename, line, "path")
                    )
                elif INLINE_SUPPRESSION_MARKER in text:
                    suppressed.append(
                        SuppressedFinding(rule_id, changed.filename, line, "inline")
                    )
                else:
                    findings.append(SecretFinding(rule_id, changed.filename, line))
    return ScanResult(findings, suppressed)


def compile_allow_path(pattern: str) -> re.Pattern[str]:
    """Translate a path glob: ``*`` and ``?`` stay inside one path segment, and
    ``**`` crosses ``/`` (``tests/fixtures/**``, ``**/*.pem``)."""
    parts: list[str] = []
    position = 0
    while position < len(pattern):
        if pattern.startswith("**/", position):
            parts.append("(?:.*/)?")
            position += 3
        elif pattern.startswith("**", position):
            parts.append(".*")
            position += 2
        elif pattern[position] == "*":
            parts.append("[^/]*")
            position += 1
        elif pattern[position] == "?":
            parts.append("[^/]")
            position += 1
        else:
            parts.append(re.escape(pattern[position]))
            position += 1
    return re.compile("".join(parts))


def withhold_suppressed(
    files: Sequence[ChangedFile], suppressed: Sequence[SuppressedFinding]
) -> list[ChangedFile]:
    """Copy of ``files`` with each suppressed added line replaced by a stub, so
    a line the scanner flagged never reaches the AI provider (ADR-0004,
    ADR-0008). Line numbers and hunk structure stay intact."""
    withheld: dict[str, set[int]] = {}
    key_headers: dict[str, set[int]] = {}
    for finding in suppressed:
        withheld.setdefault(finding.filename, set()).add(finding.line)
        if finding.rule_id == PEM_RULE_ID:
            key_headers.setdefault(finding.filename, set()).add(finding.line)
    result = []
    for changed in files:
        lines = withheld.get(changed.filename)
        if lines and changed.patch:
            changed = replace(
                changed,
                patch=_withhold_lines(
                    changed.patch, lines, key_headers.get(changed.filename, set())
                ),
            )
        result.append(changed)
    return result


def _withhold_lines(patch: str, lines: set[int], key_headers: set[int]) -> str:
    """Replace the flagged added lines. A suppressed PEM header flags only its
    own line, but the key material follows on lines no rule flags, so the added
    lines after it are withheld too, up to and including the END line."""
    rows = patch.split("\n")
    in_key = False
    previous = -2
    for index, line in list(_added_rows(rows)):
        if index != previous + 1:
            in_key = False  # a context or removed line ended the added block
        previous = index
        if line in lines:
            in_key = line in key_headers
            rows[index] = WITHHELD_LINE
        elif in_key:
            in_key = _PEM_END_RE.search(rows[index]) is None
            rows[index] = WITHHELD_LINE
    return "\n".join(rows)


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
