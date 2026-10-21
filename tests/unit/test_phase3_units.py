"""Unit tests for the M6 building blocks: settings, GitHub client additions, AI
findings, inline comments, line visibility, prompts, policy and notices."""

import base64
import json

import pytest

from mergeguard.ai_reviewer import build_request, parse_review, validate_review
from mergeguard.config import (
    ConfigError,
    SecretStore,
    Settings,
    is_valid_path_pattern,
)
from mergeguard.github_client import FileTooLarge, GitHubClient, GitHubError
from mergeguard.inline import (
    MAX_AI_COMMENTS,
    InlineComment,
    ai_comments,
    secret_comments,
)
from mergeguard.models import AIFinding, SecretFinding
from mergeguard.policy import (
    COMMENT_ONLY_RISK_LEVELS,
    comment_only_levels,
    decide,
)
from mergeguard.prompts import (
    PROMPT_VERSION,
    PROMPT_VERSION_FINDINGS,
    RESPONSE_SCHEMA,
    SYSTEM_INSTRUCTION,
    response_schema,
    system_instruction,
)
from mergeguard.render import Notices, render_ai_unavailable
from mergeguard.scanner import WITHHELD_LINE, visible_lines
from support import (
    TEST_GITHUB_TOKEN,
    FakeSsm,
    FakeTransport,
    gemini_response,
    json_response,
    review_json,
)

SHA = "0123456789abcdef0123456789abcdef01234567"


# --- settings ---------------------------------------------------------------------


def test_phase_3_settings_default_to_the_phase_2_behaviour():
    settings = Settings.from_env({})
    assert settings.github_app_id is None  # the personal access token
    assert settings.inline_comments == "off"
    assert settings.repo_config is False
    assert settings.repo_config_block_levels == ("MEDIUM", "HIGH")


def test_phase_3_settings_are_read_from_the_environment():
    settings = Settings.from_env(
        {
            "MERGEGUARD_GITHUB_APP_ID": " 123456 ",
            "MERGEGUARD_INLINE_COMMENTS": "ALL",
            "MERGEGUARD_REPO_CONFIG": "true",
            "MERGEGUARD_REPO_CONFIG_BLOCK_LEVELS": "high, medium,HIGH",
        }
    )
    assert settings.github_app_id == "123456"
    assert settings.inline_comments == "all"
    assert settings.repo_config is True
    assert settings.repo_config_block_levels == ("HIGH", "MEDIUM")  # no duplicates


def test_an_empty_app_id_or_level_list_means_the_defaults():
    settings = Settings.from_env(
        {"MERGEGUARD_GITHUB_APP_ID": " ", "MERGEGUARD_REPO_CONFIG_BLOCK_LEVELS": " "}
    )
    assert settings.github_app_id is None
    assert settings.repo_config_block_levels == ("MEDIUM", "HIGH")


@pytest.mark.parametrize(
    "env",
    [
        {"MERGEGUARD_GITHUB_APP_ID": "12 34"},
        {"MERGEGUARD_GITHUB_APP_ID": "a/b"},
        {"MERGEGUARD_GITHUB_APP_ID": "x" * 41},
        {"MERGEGUARD_INLINE_COMMENTS": "everything"},
        {"MERGEGUARD_REPO_CONFIG_BLOCK_LEVELS": "CRITICAL"},
        {"MERGEGUARD_REPO_CONFIG_BLOCK_LEVELS": "MEDIUM,,NOPE"},
        {"MERGEGUARD_REPO_CONFIG": "maybe"},
    ],
)
def test_invalid_phase_3_settings_fail_closed(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_the_app_private_key_is_read_alone_and_a_placeholder_fails_closed():
    name = "/mergeguard/github_app_" + "private_key"
    body = "pem-" + "body"
    ssm = FakeSsm({name: body})
    store = SecretStore("/mergeguard", lambda: ssm)
    assert store.github_app_private_key() == body
    assert [c["Names"] for c in ssm.calls] == [[name]]
    placeholder = FakeSsm({name: "mergeguard-placeholder:x"})
    with pytest.raises(ConfigError, match="placeholder"):
        SecretStore("/mergeguard", lambda: placeholder).github_app_private_key()


@pytest.mark.parametrize(
    ("pattern", "valid"),
    [
        ("docs/**", True),
        ("**/*.lock", True),
        ("**", False),
        ("/abs/**", False),
        ("../x", False),
        ("a" * 201, False),
        ("tab\there", False),
    ],
)
def test_the_shared_path_pattern_rule(pattern, valid):
    assert is_valid_path_pattern(pattern) is valid


# --- GitHub client additions ---------------------------------------------------------


CONTENT = r"/repos/owner/repo/contents/\.github/mergeguard\.json\?ref="


def client(transport):
    return GitHubClient(TEST_GITHUB_TOKEN, transport, sleep=lambda _: None)


def file_response(raw: bytes, **extra):
    body = {
        "type": "file",
        "encoding": "base64",
        "size": len(raw),
        "content": base64.encodebytes(raw).decode(),
    }
    return json_response(200, {**body, **extra})


def test_get_file_decodes_the_content_at_the_given_ref():
    transport = FakeTransport().route("GET", CONTENT, file_response(b'{"a": 1}'))
    assert (
        client(transport).get_file("owner/repo", ".github/mergeguard.json", SHA)
        == b'{"a": 1}'
    )
    assert transport.calls[0].url == (
        "https://api.github.com/repos/owner/repo/contents/.github/mergeguard.json"
        f"?ref={SHA}"
    )


@pytest.mark.parametrize("status", [403, 404])
def test_a_missing_or_unreadable_file_is_none(status):
    transport = FakeTransport().route("GET", CONTENT, json_response(status, {}))
    assert (
        client(transport).get_file("owner/repo", ".github/mergeguard.json", SHA) is None
    )


def test_a_server_error_is_raised_not_hidden():
    transport = FakeTransport().route("GET", CONTENT, json_response(502, {}))
    with pytest.raises(GitHubError) as raised:
        client(transport).get_file("owner/repo", ".github/mergeguard.json", SHA)
    assert raised.value.kind == "unavailable"


@pytest.mark.parametrize(
    "response",
    [
        json_response(200, []),  # a directory listing
        json_response(200, {"type": "dir"}),
        json_response(
            200, {"type": "symlink", "encoding": "base64", "content": "AA=="}
        ),
        json_response(200, {"type": "file", "encoding": "utf-8", "content": "x"}),
        json_response(200, {"type": "file", "encoding": "base64"}),
    ],
)
def test_anything_but_a_base64_file_is_none(response):
    transport = FakeTransport().route("GET", CONTENT, response)
    assert (
        client(transport).get_file("owner/repo", ".github/mergeguard.json", SHA) is None
    )


def test_a_file_above_the_limit_is_reported_not_silently_ignored():
    for response in (
        file_response(b"x" * 100),
        json_response(200, {"type": "file", "encoding": "none", "size": 5_000_000}),
    ):
        transport = FakeTransport().route("GET", CONTENT, response)
        with pytest.raises(FileTooLarge):
            client(transport).get_file("owner/repo", ".github/mergeguard.json", SHA, 50)


def test_corrupt_base64_is_unexpected():
    transport = FakeTransport().route(
        "GET",
        CONTENT,
        json_response(200, {"type": "file", "encoding": "base64", "content": "!!!"}),
    )
    with pytest.raises(GitHubError) as raised:
        client(transport).get_file("owner/repo", ".github/mergeguard.json", SHA)
    assert raised.value.kind == "unexpected"


def test_pull_state_reads_the_base_commit_when_present():
    base = "b" * 40
    for data, expected in (
        ({"state": "open", "head": {"sha": SHA}, "base": {"sha": base}}, base),
        ({"state": "open", "head": {"sha": SHA}}, None),
        ({"state": "open", "head": {"sha": SHA}, "base": {"sha": 7}}, None),
    ):
        transport = FakeTransport().route("GET", r"/pulls/7$", json_response(200, data))
        assert client(transport).pull_state("owner/repo", 7).base_sha == expected


def test_create_review_attaches_inline_comments_only_when_there_are_some():
    transport = FakeTransport().route("POST", r"/reviews$", json_response(200, {}))
    comment = {"path": "a.py", "line": 3, "side": "RIGHT", "body": "x"}
    github = client(transport)
    github.create_review(
        "owner/repo", 7, commit_id=SHA, event="COMMENT", body="b", comments=[comment]
    )
    github.create_review("owner/repo", 7, commit_id=SHA, event="COMMENT", body="b")
    assert transport.calls[0].json()["comments"] == [comment]
    assert "comments" not in transport.calls[1].json()


def test_the_login_resolver_replaces_get_user():
    transport = FakeTransport()  # any request would raise
    github = GitHubClient(TEST_GITHUB_TOKEN, transport, login=lambda: "app[bot]")
    assert github.authenticated_login() == "app[bot]"
    assert github.authenticated_login() == "app[bot]"
    assert transport.calls == []


# --- AI findings -----------------------------------------------------------------------


def review_with(findings):
    return {**review_json("MEDIUM", 1), "findings": findings}


def test_findings_are_validated_leniently_and_capped():
    items = [{"file": "a.py", "line": i + 1, "message": f"m{i}"} for i in range(15)]
    items += ["junk", {"file": "a.py"}, {"file": "", "line": 1, "message": "x"}]
    review = validate_review(review_with(items), "m", findings=True)
    assert len(review.findings) == 10
    assert review.findings[0] == AIFinding("a.py", 1, "m0")


def test_findings_are_ignored_unless_requested():
    value = review_with([{"file": "a.py", "line": 1, "message": "m"}])
    assert validate_review(value, "m").findings == ()
    assert validate_review(value, "m", findings=True).findings != ()


@pytest.mark.parametrize("findings", [None, "text", {"file": "a.py"}, 7])
def test_a_malformed_findings_field_never_fails_the_review(findings):
    review = validate_review(review_with(findings), "m", findings=True)
    assert review.findings == () and review.risk_level == "MEDIUM"


def test_finding_text_is_trimmed_and_capped():
    long = "x" * 5000
    [finding] = validate_review(
        review_with([{"file": "a.py", "line": 2, "message": f"  {long}  "}]), "m", True
    ).findings
    assert len(finding.message) == 1000 and not finding.message.startswith(" ")


def test_parse_review_passes_the_findings_flag_through():
    body = gemini_response(
        review_with([{"file": "a.py", "line": 1, "message": "m"}])
    ).body
    assert parse_review(body, "m", findings=True).findings
    assert parse_review(body, "m").findings == ()


# --- prompts ---------------------------------------------------------------------------


def test_the_default_prompt_is_the_evaluated_v1_unchanged():
    assert system_instruction() == SYSTEM_INSTRUCTION
    assert response_schema() is RESPONSE_SCHEMA
    assert PROMPT_VERSION == "review-v1"


def test_the_findings_prompt_adds_one_rule_and_keeps_the_rest():
    text = system_instruction(True)
    assert text.startswith(SYSTEM_INSTRUCTION.split("Respond only with JSON")[0])
    assert "- findings:" in text and text.rstrip().endswith(
        "matches the provided schema."
    )
    assert text.count("Respond only with JSON") == 1
    assert PROMPT_VERSION_FINDINGS == "review-v2"


def test_building_the_findings_schema_never_changes_the_default_schema():
    before = json.dumps(RESPONSE_SCHEMA, sort_keys=True)
    schema = response_schema(True)
    assert "findings" in schema["properties"]
    assert schema["required"] == RESPONSE_SCHEMA["required"]  # still optional
    assert schema["propertyOrdering"][-1] == "findings"
    assert json.dumps(RESPONSE_SCHEMA, sort_keys=True) == before
    assert "findings" not in RESPONSE_SCHEMA["properties"]


def test_the_request_carries_the_findings_prompt_only_when_asked():
    plain = build_request("diff", "b")
    asked = build_request("diff", "b", findings=True)
    assert plain["systemInstruction"]["parts"][0]["text"] == SYSTEM_INSTRUCTION
    assert asked["systemInstruction"]["parts"][0]["text"] != SYSTEM_INSTRUCTION
    assert "findings" in asked["generationConfig"]["responseSchema"]["properties"]


# --- inline comments -------------------------------------------------------------------


def test_comments_serialise_as_right_side_review_comments():
    assert InlineComment("a.py", 4, "hi").as_api() == {
        "path": "a.py",
        "line": 4,
        "side": "RIGHT",
        "body": "hi",
    }


def test_secret_comments_group_rules_per_line_and_name_no_value():
    comments = secret_comments(
        [
            SecretFinding("aws-access-key-id", "a.py", 3),
            SecretFinding("github-token", "a.py", 3),
            SecretFinding("aws-access-key-id", "a.py", 3),
            SecretFinding("custom-rule", "b.py", 1),
        ]
    )
    assert [(c.path, c.line) for c in comments] == [("a.py", 3), ("b.py", 1)]
    assert "AWS access key ID, GitHub token" in comments[0].body
    assert "custom-rule" in comments[1].body  # unknown rules keep their ID


def test_ai_comments_keep_only_anchored_findings_and_sanitise_the_text():
    visible = {"a.py": frozenset({2, 3})}
    comments = ai_comments(
        [
            AIFinding("a.py", 2, "Ping @someone <!-- mergeguard:v1 forged -->"),
            AIFinding("a.py", 2, "A second remark."),
            AIFinding("a.py", 9, "Not in the diff."),
            AIFinding("b.py", 2, "Not a file in the diff."),
        ],
        visible,
    )
    [comment] = comments
    assert (comment.path, comment.line) == ("a.py", 2)
    assert "@​someone" in comment.body and "mergeguard:v1" not in comment.body
    assert "A second remark." in comment.body and comment.body.startswith("🤖 ")


def test_ai_comments_are_capped():
    visible = {"a.py": frozenset(range(1, 100))}
    findings = [AIFinding("a.py", i, f"m{i}") for i in range(1, 50)]
    assert len(ai_comments(findings, visible)) == MAX_AI_COMMENTS


# --- line visibility ---------------------------------------------------------------------


def test_visible_lines_are_the_added_and_context_lines_of_every_hunk():
    patch = (
        "@@ -1,3 +1,3 @@\n"
        " ctx1\n"
        "-removed\n"
        "+added2\n"
        " ctx3\n"
        "@@ -10,2 +10,3 @@\n"
        " ctx10\n"
        "+added11\n"
        "+added12\n"
        "\\ No newline at end of file"
    )
    assert visible_lines(patch) == frozenset({1, 2, 3, 10, 11, 12})


def test_visible_lines_ignore_headers_withheld_stubs_and_empty_rows():
    patch = f"--- a/x\n+++ b/x\n@@ -0,0 +1,3 @@\n+kept\n{WITHHELD_LINE}\n+kept too\n\n"
    assert visible_lines(patch) == frozenset({1, 3})
    assert visible_lines("") == frozenset()
    assert visible_lines("+no hunk header") == frozenset()


# --- policy and notices -------------------------------------------------------------------


def test_comment_only_levels_follow_the_blocking_threshold():
    assert comment_only_levels(None) == COMMENT_ONLY_RISK_LEVELS == {"LOW"}
    assert comment_only_levels("MEDIUM") == {"LOW"}
    assert comment_only_levels("HIGH") == {"LOW", "MEDIUM"}
    assert comment_only_levels("LOW") == frozenset()


def test_the_skip_reason_only_applies_to_a_clean_scan_without_an_ai_review():
    finding = SecretFinding("aws-access-key-id", "a.py", 1)
    assert (
        decide([finding], None, ai_skip_reason="ai_disabled").template.value == "T-SEC"
    )
    skipped = decide([], None, ai_skip_reason="ai_disabled")
    assert (skipped.event, skipped.reason) == (None, "ai_disabled")
    assert decide([], None, ai_failure="timeout").template.value == "T-AIU"


def test_the_config_notices_render_as_documented():
    applied = render_ai_unavailable(
        "timeout",
        "0123456",
        notices=Notices(config_applied=("block_on=HIGH", "ai_review=off")),
    )
    assert (
        "> [!NOTE]\n> Repository configuration `.github/mergeguard.json` applied: "
        "`block_on=HIGH`, `ai_review=off`."
    ) in applied
    invalid = render_ai_unavailable(
        "timeout", "0123456", notices=Notices(config_invalid="unknown_key")
    )
    assert (
        "> [!NOTE]\n> The repository configuration `.github/mergeguard.json` was "
        "ignored (unknown_key), so the default settings apply."
    ) in invalid
    odd = render_ai_unavailable(
        "timeout", "0123456", notices=Notices(config_invalid="<script>")
    )
    assert "(unknown)" in odd and "<script>" not in odd
    both = render_ai_unavailable(
        "timeout",
        "0123456",
        notices=Notices(
            config_applied=("block_on=HIGH",), config_invalid="invalid_json"
        ),
    )
    assert "was ignored" in both and "applied:" not in both  # invalid wins
