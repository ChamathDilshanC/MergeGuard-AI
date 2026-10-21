"""Enforce ADR-0002: runtime code imports only the standard library and boto3/botocore.

This is quality gate 5 in docs/TESTING_STRATEGY.md section 9 (NFR-008).
"""

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
ALLOWED_THIRD_PARTY = frozenset({"boto3", "botocore"})
SOURCE_FILES = sorted(SRC.rglob("*.py"))


def first_party_names() -> frozenset[str]:
    return frozenset(
        p.stem
        for p in SRC.iterdir()
        if p.suffix == ".py" or (p / "__init__.py").is_file()
    )


def disallowed_imports(source: str, first_party: frozenset[str]) -> set[str]:
    allowed = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY | first_party
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    return imported - allowed


def test_source_tree_is_not_empty():
    # Guards against the gate below passing vacuously.
    assert SOURCE_FILES


@pytest.mark.parametrize(
    "path", SOURCE_FILES, ids=lambda p: p.relative_to(SRC).as_posix()
)
def test_runtime_imports_are_stdlib_or_boto3(path: Path):
    source = path.read_text(encoding="utf-8")
    assert disallowed_imports(source, first_party_names()) == set()


def test_checker_flags_third_party_imports():
    snippet = (
        "import json\n"
        "import boto3.session\n"
        "import requests\n"
        "from yaml import safe_load\n"
        "from . import sibling\n"
        "from mergeguard import config\n"
    )
    assert disallowed_imports(snippet, frozenset({"mergeguard"})) == {
        "requests",
        "yaml",
    }
