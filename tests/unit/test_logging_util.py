"""Allow-listed JSON logging (FR-013)."""

import io
import json
import re

from mergeguard.logging_util import ALLOWED_FIELDS, JsonLogger


def records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_writes_one_json_object_per_line():
    stream = io.StringIO()
    JsonLogger("INFO", stream, delivery_id="d-1").log(
        "review.posted", decision="COMMENT"
    )
    [record] = records(stream)
    assert record["event"] == "review.posted"
    assert record["level"] == "INFO"
    assert record["delivery_id"] == "d-1"
    assert record["decision"] == "COMMENT"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", record["ts"])


def test_unknown_and_none_fields_are_dropped():
    stream = io.StringIO()
    logger = JsonLogger("INFO", stream, body="raw body")
    logger.log("webhook.received", body="raw body", signature="sha256=x", reason=None)
    [record] = records(stream)
    assert set(record) == {"ts", "level", "event"}


def test_bind_adds_context_to_every_record():
    stream = io.StringIO()
    logger = JsonLogger("INFO", stream, delivery_id="d-1").bind(repo="owner/repo", pr=7)
    logger.log("scan.completed", rule_ids=["github-token"])
    [record] = records(stream)
    assert (record["delivery_id"], record["repo"], record["pr"]) == (
        "d-1",
        "owner/repo",
        7,
    )
    assert record["rule_ids"] == ["github-token"]


def test_level_threshold():
    stream = io.StringIO()
    logger = JsonLogger("WARNING", stream)
    logger.log("debug.event", "DEBUG")
    logger.log("info.event")
    logger.log("error.event", "ERROR")
    assert [r["event"] for r in records(stream)] == ["error.event"]


def test_defaults_to_stdout(capsys):
    JsonLogger().log("webhook.received")
    assert json.loads(capsys.readouterr().out)["event"] == "webhook.received"


def test_allow_list_matches_the_design_system():
    assert {
        "delivery_id",
        "repo",
        "pr",
        "head_sha",
        "decision",
        "rule_ids",
    } <= ALLOWED_FIELDS
    assert not {"body", "token", "signature", "patch", "diff"} & ALLOWED_FIELDS
