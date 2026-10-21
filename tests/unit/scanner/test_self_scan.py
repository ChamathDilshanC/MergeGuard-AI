"""The repository must not trip its own scanner (fake-secret policy,
docs/TESTING_STRATEGY.md section 4). Only AWS's documented example key ID is
allowed, because the policy uses it on purpose."""

from pathlib import Path

from mergeguard.scanner import matching_rules
from support import AWS_EXAMPLE_KEY_ID

ROOT = Path(__file__).resolve().parents[3]
SCANNED_SUFFIXES = {
    ".py",
    ".md",
    ".tf",
    ".hcl",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
    ".txt",
}
SKIPPED_DIRS = {
    ".git",
    ".venv",
    ".terraform",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}


def repository_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.suffix in SCANNED_SUFFIXES
        and not SKIPPED_DIRS & set(path.relative_to(ROOT).parts)
    ]


def test_repository_scans_clean_apart_from_the_documented_example_key():
    files = repository_files()
    assert len(files) > 20  # guards against scanning nothing
    hits = [
        f"{path.relative_to(ROOT)}:{number}: {rule}"
        for path in files
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
        if AWS_EXAMPLE_KEY_ID not in line
        for rule in matching_rules(line)
    ]
    assert hits == []
