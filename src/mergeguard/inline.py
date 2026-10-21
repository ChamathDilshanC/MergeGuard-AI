"""Inline line-level review comments (FR-021, T-602).

Two sources, both anchored only to a line that is present in the diff:

- secret findings (the scanner's added lines), whose comment names the rule and
  the remediation but never the value;
- AI findings (prompt ``review-v2``), checked against the diff that was actually
  sent to the model, because a model can name a line that does not exist.

The comments travel in the same API call as the review, so GitHub accepts them
together or not at all. If it refuses them, the pipeline posts the review without
them; the summary review is always the source of truth.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from mergeguard.models import AIFinding, SecretFinding
from mergeguard.render import RULE_LABELS, sanitize

MAX_SECRET_COMMENTS = 25
MAX_AI_COMMENTS = 10
MAX_COMMENT_CHARS = 1000


@dataclass(frozen=True, slots=True)
class InlineComment:
    path: str
    line: int
    body: str

    def as_api(self) -> dict[str, object]:
        """The review comment as GitHub expects it: the new side of the diff."""
        return {
            "path": self.path,
            "line": self.line,
            "side": "RIGHT",
            "body": self.body,
        }


def secret_comments(findings: Sequence[SecretFinding]) -> list[InlineComment]:
    """One comment per flagged line, naming each rule that matched it. Added lines
    are always part of the diff, so these anchors need no further check."""
    grouped: dict[tuple[str, int], list[str]] = {}
    for finding in findings:
        grouped.setdefault((finding.filename, finding.line), []).append(
            RULE_LABELS.get(finding.rule_id, finding.rule_id)
        )
    comments = []
    for (path, line), labels in list(grouped.items())[:MAX_SECRET_COMMENTS]:
        found = ", ".join(dict.fromkeys(labels))
        comments.append(
            InlineComment(
                path,
                line,
                f"🚨 **Possible credential: {found}.** Revoke or rotate it at its "
                "provider first: it is already visible in this branch's history. "
                "Then load it from AWS SSM Parameter Store or an environment "
                "variable.",
            )
        )
    return comments


def ai_comments(
    findings: Sequence[AIFinding], visible: Mapping[str, frozenset[int]]
) -> list[InlineComment]:
    """AI findings whose file and line are in the diff the model was sent. The
    others are dropped without a trace in the review: they are the model's
    mistakes, not the author's. The text is sanitised like the rest of its output
    (mentions, headings and markers, DESIGN_SYSTEM.md section 5.7)."""
    grouped: dict[tuple[str, int], list[str]] = {}
    for finding in findings:
        if finding.line not in visible.get(finding.file, frozenset()):
            continue
        text = sanitize(" ".join(finding.message.split()), MAX_COMMENT_CHARS)
        if text:
            grouped.setdefault((finding.file, finding.line), []).append(text)
    return [
        InlineComment(path, line, "🤖 " + "\n\n".join(texts))
        for (path, line), texts in list(grouped.items())[:MAX_AI_COMMENTS]
    ]
