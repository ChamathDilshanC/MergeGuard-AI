"""Terraform and the Lambda code must agree (T-401). Fails on drift between
terraform/main.tf and src/: environment variables, secret names, the
placeholder, runtime and handler."""

import re
from pathlib import Path

import lambda_function
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


def test_lambda_environment_matches_the_settings_the_code_reads():
    environment = _block("environment {")
    set_by_terraform = set(re.findall(r"(MERGEGUARD_[A-Z_]+)\s*=", environment))
    read_by_code = set(re.findall(r'"(MERGEGUARD_[A-Z_]+)"', CONFIG_SOURCE))
    assert set_by_terraform == read_by_code


def test_terraform_creates_exactly_the_secrets_the_code_reads():
    names = re.search(r"secret_names = toset\(\[(.*?)\]\)", MAIN_TF).group(1)
    created = set(re.findall(r'"([a-z_]+)"', names))
    assert created == {*config.CORE_SECRET_NAMES, config.GEMINI_KEY_NAME}


def test_placeholder_is_recognised_by_the_code():
    placeholder = re.search(r'secret_placeholder = "([^"]+)"', MAIN_TF).group(1)
    assert placeholder.startswith(config.PLACEHOLDER_PREFIX)


def test_secret_values_are_write_only():
    """ADR-0007: the value must never be a state-stored attribute."""
    parameter = _block('resource "aws_ssm_parameter" "secret"')
    assert "value_wo " in parameter
    assert not re.search(r"^\s*(value|insecure_value)\s*=", parameter, re.MULTILINE)


def test_runtime_and_handler_match_the_code():
    function = _block('resource "aws_lambda_function" "reviewer"')
    pin = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert f'runtime          = "python{pin}"' in function
    assert 'handler          = "lambda_function.lambda_handler"' in function
    assert callable(lambda_function.lambda_handler)
