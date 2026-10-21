"""Per-repository configuration, ``.github/mergeguard.json`` (FR-022, ADR-0011).

JSON, not YAML, to keep zero runtime dependencies (ADR-0002). The file is read from
the pull request's **base commit**, never its head, so the author of a pull request
cannot change the rules that apply to it. It can only narrow or relax what the
operator allows:

- ``block_on``: the lowest AI risk level that requests changes, within the levels
  the operator permits. AI bugs and secret findings still block.
- ``ai_review``: ``false`` keeps the diff away from the AI for this repository.
- ``ignore_paths``: path globs that are not sent to the AI.
- ``inline_comments``: ``off``, ``secrets`` or ``all``, never above the operator's mode.
- ``language``: only ``en`` exists until the language question (OQ-03) is decided.

What it can never touch: the secret scan, its suppressions (ADR-0008), the
no-approve rule, and anything about identity or secrets. An invalid file is
ignored as a whole, and the review says so (``N-CONFIG-INVALID``); it is never half
applied.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass

from mergeguard.config import (
    INLINE_COMMENT_MODES,
    MAX_ALLOW_PATHS,
    is_valid_path_pattern,
)

CONFIG_PATH = ".github/mergeguard.json"
MAX_CONFIG_BYTES = 16_384
SUPPORTED_VERSION = 1
LANGUAGES = frozenset({"en"})
KNOWN_KEYS = frozenset(
    {"version", "block_on", "ai_review", "ignore_paths", "inline_comments", "language"}
)


@dataclass(frozen=True, slots=True)
class RepoConfig:
    """The effective repository settings. ``None`` means "use the operator's"."""

    block_on: str | None = None
    ai_review: bool = True
    ignore_paths: tuple[str, ...] = ()
    inline_comments: str | None = None
    applied: tuple[str, ...] = ()  # shown in the review (N-CONFIG)


@dataclass(frozen=True, slots=True)
class ConfigOutcome:
    config: RepoConfig
    invalid: str | None = None  # a reason category, never file content


class _Invalid(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_repo_config(
    raw: bytes, *, block_levels: Sequence[str], inline_mode: str
) -> ConfigOutcome:
    """Validate ``raw`` against the operator's bounds. Any problem makes the whole
    file invalid, so the defaults apply instead of a half-read file."""
    try:
        return ConfigOutcome(_parse(raw, block_levels, inline_mode))
    except _Invalid as exc:
        return ConfigOutcome(RepoConfig(), exc.reason)


def _parse(raw: bytes, block_levels: Sequence[str], inline_mode: str) -> RepoConfig:
    if len(raw) > MAX_CONFIG_BYTES:
        raise _Invalid("too_large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _Invalid("invalid_json") from exc
    if not isinstance(data, dict):
        raise _Invalid("invalid_json")
    if set(data) - KNOWN_KEYS:
        raise _Invalid("unknown_key")
    if "version" in data and (
        type(data["version"]) is not int or data["version"] != SUPPORTED_VERSION
    ):
        raise _Invalid("unsupported_version")

    applied: list[str] = []
    block_on = data.get("block_on")
    if "block_on" in data:
        if not isinstance(block_on, str):
            raise _Invalid("invalid_field")
        block_on = block_on.strip().upper()
        if block_on not in block_levels:
            raise _Invalid("out_of_bounds")
        applied.append(f"block_on={block_on}")

    ai_review = data.get("ai_review", True)
    if type(ai_review) is not bool:
        raise _Invalid("invalid_field")
    if "ai_review" in data:
        applied.append(f"ai_review={'on' if ai_review else 'off'}")

    ignore_paths = data.get("ignore_paths", [])
    if (
        not isinstance(ignore_paths, list)
        or len(ignore_paths) > MAX_ALLOW_PATHS
        or not all(
            isinstance(item, str) and is_valid_path_pattern(item)
            for item in ignore_paths
        )
    ):
        raise _Invalid("invalid_field")
    if ignore_paths:
        applied.append(f"ignore_paths={len(ignore_paths)} pattern(s)")

    inline = data.get("inline_comments")
    if "inline_comments" in data:
        if not isinstance(inline, str) or inline not in INLINE_COMMENT_MODES:
            raise _Invalid("invalid_field")
        # A repository may switch inline comments down, never above the operator.
        if INLINE_COMMENT_MODES.index(inline) > INLINE_COMMENT_MODES.index(inline_mode):
            raise _Invalid("out_of_bounds")
        applied.append(f"inline_comments={inline}")

    if "language" in data:
        if not isinstance(data["language"], str):
            raise _Invalid("invalid_field")
        if data["language"] not in LANGUAGES:
            raise _Invalid("out_of_bounds")

    return RepoConfig(
        block_on=block_on,
        ai_review=ai_review,
        ignore_paths=tuple(ignore_paths),
        inline_comments=inline,
        applied=tuple(applied),
    )
