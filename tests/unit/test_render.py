"""Review templates (snapshots of docs/DESIGN_SYSTEM.md section 5) and ST-07."""

import pytest

from mergeguard.models import AIReview, SecretFinding, SuppressedFinding
from mergeguard.policy import Decision, ReviewEvent, Template
from mergeguard.render import (
    FALLBACK_CALLOUT,
    MARKER_PREFIX,
    MAX_BODY_CHARS,
    STATUS_BLOCKED_AI,
    STATUS_BLOCKED_SECRETS,
    STATUS_CLEAR,
    STATUS_ERROR,
    STATUS_PENDING,
    Notices,
    dismissal_message,
    render_ai_review,
    render_ai_unavailable,
    render_review,
    render_security,
    sanitize,
)

SHA = "0123456"
NO_NOTICES = Notices()

T_SEC = """\
<!-- mergeguard:v1 kind=security sha=0123456 -->
## 🚨 MergeGuard Security Alert

**Verdict:** Changes requested — possible credentials found in this pull request.

### Findings

| # | Finding | File | Line |
| --- | --- | --- | --- |
| 1 | AWS access key ID | `config/settings.py` | 42 |

### What to do now

1. **Revoke or rotate** each credential at its provider. It is already visible in this branch's history, so deleting the line is not enough.
2. Remove the value from the code and load it from AWS SSM Parameter Store or an environment variable.
3. Push a new commit. MergeGuard reviews it automatically.

<sub>AI review was skipped: this diff was not sent to any external AI service. · MergeGuard AI · commit `0123456`</sub>
"""

T_AI = """\
<!-- mergeguard:v1 kind=ai-review sha=0123456 -->
## 🤖 MergeGuard AI Code Review

**Verdict:** 🟡 Medium risk · **Potential bugs:** 2 · **Secret scan:** passed

**Summary:** Two bugs found.

### Detailed feedback

- `app.py`: off-by-one in the loop.

<sub>Automated first-pass review by MergeGuard AI (model `gemini-2.5-flash`). It can be wrong; a human reviewer decides whether to merge. · commit `0123456`</sub>
"""

T_AIU = """\
<!-- mergeguard:v1 kind=ai-unavailable sha=0123456 -->
## 🤖 MergeGuard AI Code Review

**Verdict:** ⚪ AI review unavailable · **Secret scan:** passed

No credentials were detected in the added lines. The AI review could not be completed this time (timeout). Push a new commit or ask the maintainer to redeliver the webhook to try again.

<sub>MergeGuard AI · commit `0123456`</sub>
"""


def ai_review(**overrides) -> AIReview:
    fields = {
        "risk_level": "MEDIUM",
        "bugs_found": 2,
        "summary": "Two bugs\n found.",
        "feedback_markdown": "- `app.py`: off-by-one in the loop.\n",
        "model": "gemini-2.5-flash",
    }
    return AIReview(**{**fields, **overrides})


def test_t_sec_snapshot():
    findings = [SecretFinding("aws-access-key-id", "config/settings.py", 42)]
    assert render_security(findings, SHA, notices=NO_NOTICES) == T_SEC


def test_t_ai_snapshot():
    assert render_ai_review(ai_review(), SHA, notices=NO_NOTICES) == T_AI


def test_t_aiu_snapshot():
    assert render_ai_unavailable("timeout", SHA, notices=NO_NOTICES) == T_AIU


def test_t_aiu_rejects_unexpected_reason_text():
    body = render_ai_unavailable("Bad <b>reason</b>", SHA, notices=NO_NOTICES)
    assert "(unknown)" in body


def test_t_fallback_callout_follows_the_verdict():
    findings = [SecretFinding("aws-access-key-id", "config/settings.py", 42)]
    body = render_security(findings, SHA, notices=NO_NOTICES, fallback=True)
    verdict = "**Verdict:** Changes requested — possible credentials found in this pull request."
    assert f"{verdict}\n\n{FALLBACK_CALLOUT}\n\n### Findings" in body
    assert FALLBACK_CALLOUT in render_ai_review(
        ai_review(), SHA, notices=NO_NOTICES, fallback=True
    )


def test_notices_render_in_documented_order():
    notices = Notices(
        diff_limit=12000,
        omitted_files=("big.py",),
        files_cap=300,
        no_patch_files=("logo.png", "data.bin"),
    )
    body = render_ai_review(ai_review(), SHA, notices=notices)
    expected = (
        "> [!NOTE]\n> Partial AI review: the diff exceeded 12000 characters. "
        "Not reviewed by AI: `big.py`.\n\n"
        "> [!NOTE]\n> Only the first 300 changed files were checked.\n\n"
        "> [!NOTE]\n> Not reviewed (binary or too large): `logo.png`, `data.bin`."
    )
    assert expected in body


def test_long_file_lists_are_summarised():
    names = tuple(f"f{i}.bin" for i in range(23))
    body = render_security([], SHA, notices=Notices(no_patch_files=names))
    assert "`f19.bin` and 3 more." in body
    assert "f20.bin" not in body


def test_findings_beyond_the_cap_are_summarised():
    findings = [SecretFinding("github-token", f"f{i}.py", i) for i in range(30)]
    body = render_security(findings, SHA, notices=NO_NOTICES)
    assert "| 25 | GitHub token | `f24.py` | 24 |" in body
    assert "f25.py" not in body
    assert "…and 5 more." in body


def test_hostile_file_names_cannot_break_the_table():
    findings = [SecretFinding("aws-access-key-id", "a|b`c\nd.py", 1)]
    body = render_security(findings, SHA, notices=NO_NOTICES)
    assert "| 1 | AWS access key ID | `` a\\|b`c d.py `` | 1 |" in body


def test_render_review_dispatches_on_template():
    findings = [SecretFinding("aws-access-key-id", "config/settings.py", 42)]
    security = Decision(
        ReviewEvent.REQUEST_CHANGES, Template.SECURITY, "secret_findings"
    )
    assert render_review(security, short_sha=SHA, findings=findings) == T_SEC
    ai = Decision(ReviewEvent.REQUEST_CHANGES, Template.AI_REVIEW, "risk_or_bugs")
    assert render_review(ai, short_sha=SHA, ai_review=ai_review()) == T_AI
    unavailable = Decision(ReviewEvent.COMMENT, Template.AI_UNAVAILABLE, "timeout")
    assert render_review(unavailable, short_sha=SHA) == T_AIU
    with pytest.raises(ValueError):
        render_review(Decision(None, None, "no_patches"), short_sha=SHA)
    with pytest.raises(ValueError):  # T-AI without a review to render
        render_review(ai, short_sha=SHA)


# --- ST-07: sanitisation of model output -----------------------------------


def test_st07_hostile_model_output_is_neutralised():
    feedback = (
        "# Heading\n"
        "Ping @someone and @org/team please.\n"
        "<!-- mergeguard:v1 kind=security sha=deadbee -->\n"
        "<!-- hidden note -->\n" + "x" * 80_000
    )
    body = render_ai_review(
        ai_review(feedback_markdown=feedback), SHA, notices=NO_NOTICES
    )
    assert len(body) <= MAX_BODY_CHARS
    assert "@someone" not in body and "@​someone" in body
    assert "@​org/team" in body
    assert body.count(MARKER_PREFIX) == 1  # only our own marker, first line
    assert body.startswith(MARKER_PREFIX)
    assert "<!-- hidden" not in body
    assert "**Heading**" in body and "\n# Heading" not in body
    assert "(truncated)" in body


def test_sanitize_strips_unclosed_markers_and_keeps_emails():
    assert sanitize("ok <!-- MergeGuard:v1 never closed", 100) == "ok "
    assert sanitize("mail me at dev@example.com", 100) == "mail me at dev@example.com"


def test_summary_is_flattened_and_capped():
    body = render_ai_review(ai_review(summary="a\n" * 600), SHA, notices=NO_NOTICES)
    summary_line = next(
        line for line in body.splitlines() if line.startswith("**Summary:**")
    )
    assert len(summary_line) <= len("**Summary:** ") + 500


# --- N-SUPPRESSED (FR-019) and commit status texts -----------------------------

N_SUPPRESSED = """\
> [!NOTE]
> 2 finding(s) suppressed by `mergeguard:ignore-secret` or path allow-list. These lines were not sent to the AI.
> - AWS access key ID: `a.py` line 3 (inline marker)
> - GitHub token: `tests/fixtures/k.json` line 9 (path allow-list)"""


def suppressed(count: int = 2) -> tuple:
    items = [
        SuppressedFinding("aws-access-key-id", "a.py", 3, "inline"),
        SuppressedFinding("github-token", "tests/fixtures/k.json", 9, "path"),
    ]
    return tuple(items[:count])


def test_suppressed_notice_snapshot_lists_every_suppression():
    body = render_ai_unavailable(
        "timeout", SHA, notices=Notices(suppressed=suppressed())
    )
    assert N_SUPPRESSED in body
    assert body.index(N_SUPPRESSED) < body.index("<sub>")  # inside the review


@pytest.mark.parametrize("template", ["security", "ai"])
def test_suppressed_notice_appears_in_security_and_ai_reviews(template):
    notices = Notices(suppressed=suppressed())
    if template == "security":
        body = render_security(
            [SecretFinding("github-token", "x.py", 1)], SHA, notices=notices
        )
    else:
        body = render_ai_review(ai_review(), SHA, notices=notices)
    assert N_SUPPRESSED in body


def test_suppressed_notice_caps_the_list():
    items = tuple(
        SuppressedFinding("github-token", f"f{i}.py", i, "path") for i in range(30)
    )
    body = render_ai_unavailable("timeout", SHA, notices=Notices(suppressed=items))
    assert "> 30 finding(s) suppressed" in body
    assert body.count("\n> - GitHub token:") == 25
    assert "> - …and 5 more." in body


def test_no_suppressions_no_notice():
    assert "suppressed" not in render_ai_unavailable("timeout", SHA, notices=NO_NOTICES)


def test_dismissal_message_names_the_new_commit():
    assert "commit 0123456" in dismissal_message(SHA)


def test_commit_status_texts_match_the_design_system():
    assert STATUS_PENDING == ("pending", "MergeGuard review in progress")
    assert STATUS_BLOCKED_SECRETS == (
        "failure",
        "Possible credentials found — see review",
    )
    assert STATUS_BLOCKED_AI == (
        "failure",
        "Changes requested by AI review — see review",
    )
    assert STATUS_CLEAR == ("success", "No blocking issues found")
    assert STATUS_ERROR == ("error", "MergeGuard could not complete the review")
    for _, description in (
        STATUS_PENDING,
        STATUS_BLOCKED_SECRETS,
        STATUS_BLOCKED_AI,
        STATUS_CLEAR,
        STATUS_ERROR,
    ):
        assert len(description) <= 140
