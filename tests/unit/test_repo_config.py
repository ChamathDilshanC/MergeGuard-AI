"""Per-repository configuration parsing (FR-022, T-603, ADR-0011): bounds are
enforced, and an invalid file falls back to the defaults as a whole."""

import json

import pytest

from mergeguard.repo_config import (
    MAX_CONFIG_BYTES,
    RepoConfig,
    parse_repo_config,
)

LEVELS = ("MEDIUM", "HIGH")


def parse(data, *, levels=LEVELS, inline="all"):
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    return parse_repo_config(raw, block_levels=levels, inline_mode=inline)


def test_an_empty_object_changes_nothing():
    outcome = parse({})
    assert outcome.invalid is None
    assert outcome.config == RepoConfig()
    assert outcome.config.applied == ()


def test_a_full_valid_file_is_applied_and_listed():
    outcome = parse(
        {
            "version": 1,
            "block_on": "high",
            "ai_review": False,
            "ignore_paths": ["docs/**", "**/*.lock"],
            "inline_comments": "secrets",
            "language": "en",
        }
    )
    assert outcome.invalid is None
    config = outcome.config
    assert (config.block_on, config.ai_review, config.inline_comments) == (
        "HIGH",
        False,
        "secrets",
    )
    assert config.ignore_paths == ("docs/**", "**/*.lock")
    assert config.applied == (
        "block_on=HIGH",
        "ai_review=off",
        "ignore_paths=2 pattern(s)",
        "inline_comments=secrets",
    )


# --- fallbacks: the whole file is ignored, with a reason category ----------------


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"{not json", "invalid_json"),
        (b"\xff\xfe", "invalid_json"),
        (b"[]", "invalid_json"),
        (b'"text"', "invalid_json"),
        (b"[" * 5000, "invalid_json"),  # deep nesting must not crash the parser
        (b" " * (MAX_CONFIG_BYTES + 1), "too_large"),
        (json.dumps({"version": 2}).encode(), "unsupported_version"),
        (json.dumps({"version": "1"}).encode(), "unsupported_version"),
        (json.dumps({"version": True}).encode(), "unsupported_version"),
        (json.dumps({"scan": False}).encode(), "unknown_key"),
        (json.dumps({"suppress_secrets": True}).encode(), "unknown_key"),
        (json.dumps({"block_on": 3}).encode(), "invalid_field"),
        (json.dumps({"ai_review": "no"}).encode(), "invalid_field"),
        (json.dumps({"ignore_paths": "docs/**"}).encode(), "invalid_field"),
        (json.dumps({"ignore_paths": [1]}).encode(), "invalid_field"),
        (json.dumps({"inline_comments": True}).encode(), "invalid_field"),
        (json.dumps({"inline_comments": "everything"}).encode(), "invalid_field"),
        (json.dumps({"language": 5}).encode(), "invalid_field"),
    ],
)
def test_an_invalid_file_falls_back_to_defaults_with_a_reason(raw, reason):
    outcome = parse(raw)
    assert outcome.invalid == reason
    assert outcome.config == RepoConfig()  # defaults, nothing half applied


def test_one_bad_field_discards_the_good_ones_too():
    outcome = parse({"block_on": "HIGH", "ai_review": False, "scan": False})
    assert outcome.invalid == "unknown_key"
    assert outcome.config.block_on is None and outcome.config.ai_review is True


# --- operator-defined bounds ------------------------------------------------------


def test_block_on_must_be_a_level_the_operator_allows():
    assert parse({"block_on": "HIGH"}, levels=("MEDIUM", "HIGH")).invalid is None
    assert parse({"block_on": "HIGH"}, levels=("MEDIUM",)).invalid == "out_of_bounds"
    assert parse({"block_on": "LOW"}).invalid == "out_of_bounds"
    assert parse({"block_on": "CRITICAL"}).invalid == "out_of_bounds"


@pytest.mark.parametrize(
    ("operator", "wanted", "valid"),
    [
        ("all", "all", True),
        ("all", "secrets", True),
        ("all", "off", True),
        ("secrets", "all", False),  # a repository cannot go above the operator
        ("secrets", "secrets", True),
        ("secrets", "off", True),
        ("off", "secrets", False),
        ("off", "all", False),
        ("off", "off", True),
    ],
)
def test_inline_comments_can_only_go_down_from_the_operators_mode(
    operator, wanted, valid
):
    outcome = parse({"inline_comments": wanted}, inline=operator)
    assert (outcome.invalid is None) is valid
    if not valid:
        assert outcome.invalid == "out_of_bounds"


def test_only_english_exists_until_the_language_question_is_decided():
    assert parse({"language": "en"}).invalid is None
    assert parse({"language": "si"}).invalid == "out_of_bounds"


@pytest.mark.parametrize(
    "patterns",
    [
        ["**"],  # would send nothing, or everything: a bare wildcard names no path
        ["*"],
        ["/etc/**"],
        ["../outside/**"],
        ["a" * 201],
        ["docs/**", "tab\there"],
        [f"dir{i}/**" for i in range(51)],
    ],
)
def test_ignore_paths_follow_the_same_rules_as_the_operators_allow_list(patterns):
    assert parse({"ignore_paths": patterns}).invalid == "invalid_field"


def test_ignore_paths_at_the_limit_are_accepted():
    patterns = [f"dir{i}/**" for i in range(50)]
    assert parse({"ignore_paths": patterns}).invalid is None


def test_a_config_at_the_size_limit_is_accepted():
    padding = " " * (MAX_CONFIG_BYTES - len(b"{}"))
    assert parse(b"{}" + padding.encode()).invalid is None


def test_the_fallback_never_echoes_file_content():
    outcome = parse(b'{"block_on": "<script>alert(1)</script>"}')
    assert "script" not in repr(outcome)
