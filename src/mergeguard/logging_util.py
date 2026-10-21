"""JSON-lines logger with allow-listed fields (FR-013).

Only the fields in docs/DESIGN_SYSTEM.md section 7 are written; anything else
passed in is dropped, so request bodies, diffs or secrets cannot reach the logs
through this helper by accident.
"""

import json
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

ALLOWED_FIELDS = frozenset(
    {
        "delivery_id",
        "github_event",
        "repo",
        "pr",
        "head_sha",
        "action",
        "status",
        "decision",
        "rule_ids",
        "risk_level",
        "model",
        "prompt_version",
        "reason",
        "http_status",
        "file_count",
        "body_length",
        "error_type",
        "duration_ms",
    }
)
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class JsonLogger:
    def __init__(
        self, level: str = "INFO", stream: TextIO | None = None, **context: Any
    ) -> None:
        self._level = level
        self._threshold = LEVELS[level]
        self._stream = stream
        self._context = _allowed(context)

    def bind(self, **fields: Any) -> "JsonLogger":
        """Return a logger that adds ``fields`` to every record."""
        return JsonLogger(self._level, self._stream, **{**self._context, **fields})

    def log(self, event: str, level: str = "INFO", **fields: Any) -> None:
        if LEVELS[level] < self._threshold:
            return
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        record = {
            "ts": timestamp.replace("+00:00", "Z"),
            "level": level,
            "event": event,
        }
        record.update(self._context)
        record.update(_allowed(fields))
        stream = self._stream if self._stream is not None else sys.stdout
        stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _allowed(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        name: value
        for name, value in fields.items()
        if name in ALLOWED_FIELDS and value is not None
    }
