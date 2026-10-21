"""Scaffold invariants for the Python toolchain (T-101, T-102)."""

import tomllib
from pathlib import Path

import mergeguard

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_package_version_matches_pyproject():
    assert mergeguard.__version__ == PYPROJECT["project"]["version"]


def test_python_pin_is_consistent_across_tooling():
    pin = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert PYPROJECT["project"]["requires-python"] == f"=={pin}.*"
    assert PYPROJECT["tool"]["ruff"]["target-version"] == "py" + pin.replace(".", "")


def test_no_runtime_dependencies_declared():
    assert PYPROJECT["project"]["dependencies"] == []
