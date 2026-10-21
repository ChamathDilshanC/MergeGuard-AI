"""Terraform and the Lambda code must agree (T-401, T-501). Fails on drift
between terraform/ and src/: environment variables, secret names, the
placeholder, runtimes and handlers, and which function may read which secret."""

import re
from pathlib import Path

import lambda_function
import worker_function
from mergeguard import config

ROOT = Path(__file__).resolve().parents[2]
MAIN_TF = (ROOT / "terraform" / "main.tf").read_text(encoding="utf-8")
CONFIG_SOURCE = (ROOT / "src" / "mergeguard" / "config.py").read_text(encoding="utf-8")


def _block(start: str) -> str:
    """The text of the HCL block that begins at ``start`` (brace-matched)."""
    index = MAIN_TF.index(start)
    depth = 0
    for position in range(MAIN_TF.index("{", index), len(MAIN_TF)):
        depth += {"{": 1, "}": -1}.get(MAIN_TF[position], 0)
        if depth == 0:
            return MAIN_TF[index : position + 1]
    raise AssertionError(f"unbalanced block: {start}")


def _environment(function: str) -> set[str]:
    block = _block(f'resource "aws_lambda_function" "{function}"')
    environment = block[block.index("environment {") :]
    return set(re.findall(r"(MERGEGUARD_[A-Z_]+)\s*=", _block_in(environment)))


def _block_in(text: str) -> str:
    depth = 0
    for position in range(text.index("{"), len(text)):
        depth += {"{": 1, "}": -1}.get(text[position], 0)
        if depth == 0:
            return text[: position + 1]
    raise AssertionError("unbalanced block")


def test_lambda_environments_cover_exactly_the_settings_the_code_reads():
    read_by_code = set(re.findall(r'"(MERGEGUARD_[A-Z_]+)"', CONFIG_SOURCE))
    receiver, worker = _environment("reviewer"), _environment("worker")
    assert receiver | worker == read_by_code
    # Each function gets what it uses, so no setting is dead weight.
    assert "MERGEGUARD_QUEUE_URL" in receiver - worker
    assert {"MERGEGUARD_COMMIT_STATUS", "MERGEGUARD_SCAN_ALLOW_PATHS"} <= worker
    assert {
        "MERGEGUARD_GITHUB_APP_ID",
        "MERGEGUARD_INLINE_COMMENTS",
        "MERGEGUARD_REPO_CONFIG",
        "MERGEGUARD_REPO_CONFIG_BLOCK_LEVELS",
    } <= worker - receiver
    assert "MERGEGUARD_QUEUE_URL" not in worker  # the worker never queues


def test_terraform_creates_exactly_the_secrets_the_code_reads():
    names = re.search(r"secret_names = toset\(\[(.*?)\]\)", MAIN_TF).group(1)
    created = set(re.findall(r'"([a-z_]+)"', names))
    assert created == {
        *config.CORE_SECRET_NAMES,
        config.GEMINI_KEY_NAME,
        config.GITHUB_APP_KEY_NAME,
    }


def test_each_function_may_read_only_the_secrets_it_uses():
    """Least privilege (SECURITY.md 5.3): the receiver reads the webhook secret;
    the worker reads the GitHub token and the Gemini key."""
    receiver = _block('resource "aws_iam_role_policy" "lambda"')
    worker = _block('resource "aws_iam_role_policy" "worker"')

    def parameters(policy: str) -> set[str]:
        return set(re.findall(r"parameter\$\{var\.param_prefix\}/([a-z_]+)", policy))

    assert parameters(receiver) == {config.WEBHOOK_SECRET_NAME}
    assert parameters(worker) == {
        config.GITHUB_TOKEN_NAME,
        config.GITHUB_APP_KEY_NAME,
        config.GEMINI_KEY_NAME,
    }
    assert "/*" not in receiver.replace("log-group", "")  # no parameter wildcard


def test_placeholder_is_recognised_by_the_code():
    placeholder = re.search(r'parameter_placeholder = "([^"]+)"', MAIN_TF).group(1)
    assert placeholder.startswith(config.PLACEHOLDER_PREFIX)


def test_secret_values_are_write_only():
    """ADR-0007: the value must never be a state-stored attribute."""
    parameter = _block('resource "aws_ssm_parameter" "secret"')
    assert "value_wo " in parameter
    assert not re.search(r"^\s*(value|insecure_value)\s*=", parameter, re.MULTILINE)


def test_runtimes_and_handlers_match_the_code():
    pin = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    receiver = _block('resource "aws_lambda_function" "reviewer"')
    worker = _block('resource "aws_lambda_function" "worker"')
    for function in (receiver, worker):
        assert f'runtime          = "python{pin}"' in function
    assert 'handler          = "lambda_function.lambda_handler"' in receiver
    assert 'handler          = "worker_function.lambda_handler"' in worker
    assert callable(lambda_function.lambda_handler)
    assert callable(worker_function.lambda_handler)


def test_both_functions_ship_the_same_package():
    """ADR-0005: one code package with two handlers, which keeps ADR-0002."""
    for name in ("reviewer", "worker"):
        function = _block(f'resource "aws_lambda_function" "{name}"')
        assert "filename         = data.archive_file.lambda.output_path" in function
        assert (
            "source_code_hash = data.archive_file.lambda.output_base64sha256"
            in function
        )


def test_the_worker_reports_item_failures_and_the_queue_has_a_dlq():
    mapping = _block('resource "aws_lambda_event_source_mapping" "worker"')
    assert '"ReportBatchItemFailures"' in mapping  # worker_function relies on it
    queue = _block('resource "aws_sqs_queue" "review_requests"')
    assert "redrive_policy" in queue and "deadLetterTargetArn" in queue
