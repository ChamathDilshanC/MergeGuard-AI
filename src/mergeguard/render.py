"""Review comment templates and sanitisation (FR-009).

Text and structure follow docs/DESIGN_SYSTEM.md section 5. Model-supplied text
(T-AI) is treated as untrusted and sanitised before it is posted.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from mergeguard.models import AIReview, SecretFinding, SuppressedFinding
from mergeguard.policy import Decision, Template

MARKER_PREFIX = "<!-- mergeguard:v1 "
MAX_BODY_CHARS = 60_000  # GitHub's limit is 65,536
MAX_SUMMARY_CHARS = 500
MAX_FEEDBACK_CHARS = 20_000
MAX_LISTED_FINDINGS = 25
MAX_LISTED_FILES = 20
TRUNCATED = "(truncated)"

RULE_LABELS = {
    "aws-access-key-id": "AWS access key ID",
    "private-key-pem": "Private key (PEM)",
    "github-token": "GitHub token",  # noqa: S105 - a label, not a credential
    "google-api-key": "Google API key",
    "generic-assignment": "Hardcoded password or secret",
}
RISK_VERDICTS = {
    "LOW": "🟢 Low risk",
    "MEDIUM": "🟡 Medium risk",
    "HIGH": "🔴 High risk",
}
# Commit status (docs/DESIGN_SYSTEM.md section 5.6): (state, description).
STATUS_PENDING = ("pending", "MergeGuard review in progress")
STATUS_BLOCKED_SECRETS = ("failure", "Possible credentials found — see review")
STATUS_BLOCKED_AI = ("failure", "Changes requested by AI review — see review")
STATUS_CLEAR = ("success", "No blocking issues found")
STATUS_ERROR = ("error", "MergeGuard could not complete the review")

SUPPRESSION_VIA = {"inline": "inline marker", "path": "path allow-list"}
FALLBACK_CALLOUT = (
    "> [!IMPORTANT]\n"
    "> Changes are requested. GitHub does not allow this bot identity to formally "
    "request changes on this pull request, so this review is posted as a comment."
)

_MARKER_RE = re.compile(r"<!--\s*mergeguard:.*?(?:-->|\Z)", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_`])@(?=[A-Za-z0-9])")
_REASON_RE = re.compile(r"[a-z][a-z_]{0,39}")


@dataclass(frozen=True, slots=True)
class Notices:
    """Coverage notices (docs/DESIGN_SYSTEM.md section 5.4)."""

    diff_limit: int | None = None  # N-PARTIAL-DIFF, set by the AI step (M3)
    omitted_files: tuple[str, ...] = ()
    files_cap: int | None = None  # N-PARTIAL-FILES
    no_patch_files: tuple[str, ...] = ()  # N-NO-PATCH
    suppressed: tuple[SuppressedFinding, ...] = ()  # N-SUPPRESSED (FR-019)


def render_review(
    decision: Decision,
    *,
    short_sha: str,
    findings: Sequence[SecretFinding] = (),
    ai_review: AIReview | None = None,
    notices: Notices = Notices(),  # noqa: B008 - immutable default
    fallback: bool = False,
) -> str:
    if decision.template is Template.SECURITY:
        return render_security(findings, short_sha, notices=notices, fallback=fallback)
    if decision.template is Template.AI_REVIEW and ai_review is not None:
        return render_ai_review(
            ai_review, short_sha, notices=notices, fallback=fallback
        )
    if decision.template is Template.AI_UNAVAILABLE:
        return render_ai_unavailable(decision.reason, short_sha, notices=notices)
    raise ValueError(f"nothing to render for {decision!r}")


def render_security(
    findings: Sequence[SecretFinding],
    short_sha: str,
    *,
    notices: Notices,
    fallback: bool = False,
) -> str:
    rows = ["| # | Finding | File | Line |", "| --- | --- | --- | --- |"]
    for index, finding in enumerate(findings[:MAX_LISTED_FINDINGS], start=1):
        label = RULE_LABELS.get(finding.rule_id, finding.rule_id)
        rows.append(
            f"| {index} | {label} | {_cell_code(finding.filename)} | {finding.line} |"
        )
    table = "\n".join(rows)
    hidden = len(findings) - MAX_LISTED_FINDINGS
    if hidden > 0:
        table += f"\n\n…and {hidden} more."
    blocks = [
        _header("security", short_sha, "🚨 MergeGuard Security Alert"),
        "**Verdict:** Changes requested — possible credentials found in this "
        "pull request.",
        *([FALLBACK_CALLOUT] if fallback else []),
        "### Findings",
        table,
        "### What to do now",
        "1. **Revoke or rotate** each credential at its provider. It is already "
        "visible in this branch's history, so deleting the line is not enough.\n"
        "2. Remove the value from the code and load it from AWS SSM Parameter Store "
        "or an environment variable.\n"
        "3. Push a new commit. MergeGuard reviews it automatically.",
        *_notice_blocks(notices),
        "<sub>AI review was skipped: this diff was not sent to any external AI "
        f"service. · MergeGuard AI · commit `{short_sha}`</sub>",
    ]
    return _finish(blocks)


def render_ai_review(
    review: AIReview,
    short_sha: str,
    *,
    notices: Notices,
    fallback: bool = False,
) -> str:
    verdict = RISK_VERDICTS.get(review.risk_level, RISK_VERDICTS["MEDIUM"])
    summary = sanitize(" ".join(review.summary.split()), MAX_SUMMARY_CHARS)
    blocks = [
        _header("ai-review", short_sha, "🤖 MergeGuard AI Code Review"),
        f"**Verdict:** {verdict} · **Potential bugs:** {review.bugs_found} · "
        "**Secret scan:** passed",
        *([FALLBACK_CALLOUT] if fallback else []),
        f"**Summary:** {summary}",
        "### Detailed feedback",
        sanitize(review.feedback_markdown.strip(), MAX_FEEDBACK_CHARS),
        *_notice_blocks(notices),
        "<sub>Automated first-pass review by MergeGuard AI (model "
        f"{_code(review.model)}). It can be wrong; a human reviewer decides whether "
        f"to merge. · commit `{short_sha}`</sub>",
    ]
    return _finish(blocks)


def render_ai_unavailable(reason: str, short_sha: str, *, notices: Notices) -> str:
    category = reason if _REASON_RE.fullmatch(reason) else "unknown"
    blocks = [
        _header("ai-unavailable", short_sha, "🤖 MergeGuard AI Code Review"),
        "**Verdict:** ⚪ AI review unavailable · **Secret scan:** passed",
        "No credentials were detected in the added lines. The AI review could not be "
        f"completed this time ({category}). Push a new commit or ask the maintainer "
        "to redeliver the webhook to try again.",
        *_notice_blocks(notices),
        f"<sub>MergeGuard AI · commit `{short_sha}`</sub>",
    ]
    return _finish(blocks)


def dismissal_message(short_sha: str) -> str:
    """Why an earlier blocking review was dismissed (FR-018); names the commit."""
    return (
        f"MergeGuard AI: superseded by the review of commit {short_sha}, which "
        "found no blocking issues. See the latest MergeGuard review."
    )


def sanitize(text: str, limit: int) -> str:
    """Neutralise untrusted Markdown (docs/DESIGN_SYSTEM.md section 5.7)."""
    text = _MARKER_RE.sub("", text)
    text = text.replace("<!--", "&lt;!--")  # no hidden content from the model
    text = _HEADING_RE.sub(lambda match: f"**{match.group(1)}**", text)
    text = _MENTION_RE.sub("@​", text)
    return _truncate(text, limit)


def _header(kind: str, short_sha: str, title: str) -> str:
    return f"{MARKER_PREFIX}kind={kind} sha={short_sha} -->\n## {title}"


def _notice_blocks(notices: Notices) -> list[str]:
    blocks = []
    if notices.diff_limit is not None:
        blocks.append(
            f"> [!NOTE]\n> Partial AI review: the diff exceeded {notices.diff_limit} "
            f"characters. Not reviewed by AI: {_file_list(notices.omitted_files)}."
        )
    if notices.files_cap is not None:
        blocks.append(
            f"> [!NOTE]\n> Only the first {notices.files_cap} changed files "
            "were checked."
        )
    if notices.no_patch_files:
        blocks.append(
            "> [!NOTE]\n> Not reviewed (binary or too large): "
            f"{_file_list(notices.no_patch_files)}."
        )
    if notices.suppressed:
        blocks.append(_suppressed_notice(notices.suppressed))
    return blocks


def _suppressed_notice(suppressed: Sequence[SuppressedFinding]) -> str:
    """Every suppression is listed, so a human can see what was waved through."""
    lines = [
        "> [!NOTE]",
        f"> {len(suppressed)} finding(s) suppressed by `mergeguard:ignore-secret` "
        "or path allow-list. These lines were not sent to the AI.",
    ]
    for item in suppressed[:MAX_LISTED_FINDINGS]:
        label = RULE_LABELS.get(item.rule_id, item.rule_id)
        via = SUPPRESSION_VIA.get(item.via, item.via)
        lines.append(f"> - {label}: {_code(item.filename)} line {item.line} ({via})")
    hidden = len(suppressed) - MAX_LISTED_FINDINGS
    if hidden > 0:
        lines.append(f"> - …and {hidden} more.")
    return "\n".join(lines)


def _file_list(names: Sequence[str]) -> str:
    listed = ", ".join(_code(name) for name in names[:MAX_LISTED_FILES])
    hidden = len(names) - MAX_LISTED_FILES
    return listed + (f" and {hidden} more" if hidden > 0 else "")


def _code(text: str) -> str:
    """Inline code that survives backticks and line breaks in file names."""
    text = " ".join(text.splitlines())
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def _cell_code(text: str) -> str:
    return _code(text).replace("|", "\\|")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    suffix = f"\n\n{TRUNCATED}"
    return text[: limit - len(suffix)].rstrip() + suffix


def _finish(blocks: Sequence[str]) -> str:
    return _truncate("\n\n".join(blocks) + "\n", MAX_BODY_CHARS)
