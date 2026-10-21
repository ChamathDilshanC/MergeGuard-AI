"""Secret scanner corpus (docs/TESTING_STRATEGY.md section 4, FR-005, ADR-0004).

Gates: 100% of positives detected, 0% of negatives flagged (stricter than the
documented <= 5%). Includes regressions for RB-02 and RB-03.
"""

import pytest

from mergeguard.models import ChangedFile, SecretFinding
from mergeguard.scanner import added_lines, matching_rules, scan_files, scan_patch
from support import (
    AWS_EXAMPLE_KEY_ID,
    fake_github_token,
    fake_google_api_key,
    pem_header,
)

# Short names keep this file's own source from looking like quoted secret
# assignments when MergeGuard scans this repository (fake-secret policy).
AWS = AWS_EXAMPLE_KEY_ID
ASIA = "ASIA" + AWS[4:]  # temporary-credential variant derived from the example
GKEY = fake_google_api_key()
GHP, GHO, GHS, GHU, GHR = (
    fake_github_token(p) for p in ("ghp", "gho", "ghs", "ghu", "ghr")
)
PAT = "github_" + "pat_" + "A1b2C3d4E5f6G7h8I9j0K1" + "_" + "x" * 59
VALUE = "Zq7-" + "mK2v" + "-91xR"  # secret-looking, no placeholder markers

POSITIVE = [
    ("aws-access-key-id", f'aws_access_key_id = "{AWS}"'),
    ("aws-access-key-id", f"export AWS_ACCESS_KEY_ID={AWS}"),
    ("aws-access-key-id", f"  access_key_id: {AWS}  # yaml"),
    ("aws-access-key-id", f'session = "{ASIA}"'),
    # RB-02: the brief's pattern only matched "BEGIN PRIVATE KEY".
    ("private-key-pem", pem_header("RSA ")),
    ("private-key-pem", pem_header("EC ")),
    ("private-key-pem", pem_header("DSA ")),
    ("private-key-pem", pem_header("OPENSSH ")),
    ("private-key-pem", pem_header("ENCRYPTED ")),
    ("private-key-pem", pem_header("")),  # PKCS#8
    ("private-key-pem", 'key = """' + pem_header("RSA ")),
    ("private-key-pem", "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----"),
    ("github-token", f'token = "{GHP}"'),
    ("github-token", f"GH_TOKEN={GHO}"),
    ("github-token", f"Authorization: Bearer {GHS}"),
    ("github-token", f"'{GHU}'"),
    ("github-token", f"refresh={GHR}"),
    ("github-token", f'pat = "{PAT}"'),
    ("google-api-key", f'GOOGLE_API_KEY = "{GKEY}"'),
    ("google-api-key", f"https://maps.example.com/api?key={GKEY}&q=1"),
    ("google-api-key", f'{{"key": "{GKEY}"}}'),
    ("generic-assignment", 'password = "' + VALUE + '"'),
    ("generic-assignment", 'DB_PASSWORD="' + VALUE + '"'),
    ("generic-assignment", '"apiKey": "' + VALUE + '",'),
    ("generic-assignment", "client_secret = '" + VALUE + "'"),
    ("generic-assignment", 'self.auth_token = "' + VALUE + '"'),
    ("generic-assignment", 'SECRET_KEY = "' + VALUE + '"'),
    ("generic-assignment", 'aws_secret_access_key = "' + VALUE + '"'),
    ("generic-assignment", '  githubToken: "' + VALUE + '"'),
]

NEGATIVE = [
    # placeholders
    'password = "<your-password>"',
    'password = "${DB_PASSWORD}"',
    'api_key = "YOUR_API_KEY_HERE"',
    'token = "xxxxxxxxxxxx"',
    'secret = "changeme123"',
    'password = "{{ vault_password }}"',
    'password = "EXAMPLE_PASSWORD"',
    'api_key = "%(api_key)s-value"',
    # regression from self-scan: format/f-string templates are not secrets
    'password = "{db_password}"',
    # short values
    'password = "short"',
    "AKIA1234",
    "ghp_" + "x" * 10,
    # environment lookups
    'password = os.environ["DB_PASSWORD"]',
    'token = os.getenv("GITHUB_TOKEN")',
    # names that only contain a sensitive word
    'tokenizer = "bert-base-uncased"',
    'max_tokens = "4096000000"',
    'token_type = "bearer_token"',
    'password_hash = "pbkdf2_sha256_abcdefgh"',
    'public_key = "' + VALUE + '"',
    # comparisons are not assignments
    'if password == "' + VALUE + '":',
    # documentation of patterns, including the brief's own scanner
    '"AWS Access Key": r"AKIA[0-9A-Z]{16}"',
    '"Private Key": r"-----BEGIN (RSA|OPENSSH|PRIVATE) KEY-----"',
    "Classic GitHub tokens start with ghp_ and API keys with AIza.",
    # look-alikes
    "X" + AWS,
    "-----BEGIN PUBLIC KEY-----",
    "-----BEGIN CERTIFICATE-----",
]


@pytest.mark.parametrize(("rule_id", "line"), POSITIVE, ids=lambda v: v[:40])
def test_positive_corpus_is_detected(rule_id, line):
    assert matching_rules(line) == [rule_id]


@pytest.mark.parametrize("line", NEGATIVE, ids=lambda v: v[:40])
def test_negative_corpus_is_not_flagged(line):
    assert matching_rules(line) == []


def test_corpus_has_three_variants_per_rule():
    counts: dict[str, int] = {}
    for rule_id, _ in POSITIVE:
        counts[rule_id] = counts.get(rule_id, 0) + 1
    assert set(counts) == {
        "aws-access-key-id",
        "private-key-pem",
        "github-token",
        "google-api-key",
        "generic-assignment",
    }
    assert min(counts.values()) >= 3


# --- diff structure (RB-03) -------------------------------------------------


def test_only_added_lines_are_scanned():
    patch = "\n".join(
        [
            "@@ -1,3 +1,3 @@",
            f" context = '{AWS}'",
            f"-removed = '{AWS}'",
            "+added = 1",
        ]
    )
    assert scan_patch("app.py", patch) == []


def test_removing_a_secret_is_not_flagged():
    """RB-03: the brief blocked PRs that delete a leaked key."""
    patch = f"@@ -1,2 +1,1 @@\n-aws_key = '{AWS}'\n keep = True"
    assert scan_patch("settings.py", patch) == []


def test_file_headers_before_the_first_hunk_are_skipped():
    patch = "\n".join(
        [
            f"--- a/{AWS}.txt",
            f"+++ b/{AWS}.txt",
            "@@ -0,0 +1 @@",
            "+harmless = True",
        ]
    )
    assert scan_patch("notes.txt", patch) == []


def test_plus_plus_plus_inside_a_hunk_is_an_added_line():
    # The added content is "++ <key>", so the patch line starts with "+++".
    patch = f"@@ -0,0 +1 @@\n+++ {AWS}"
    assert scan_patch("counter.c", patch) == [
        SecretFinding("aws-access-key-id", "counter.c", 1)
    ]


def test_line_numbers_follow_the_new_file_across_hunks():
    patch = "\n".join(
        [
            "@@ -10,4 +10,5 @@",
            " context one",
            f"-removed = '{AWS}'",
            "+added = 'clean'",
            f"+key = '{AWS}'",
            " context two",
            "@@ -40,2 +41,3 @@",
            " ctx",
            '+password = "' + VALUE + '"',
            "\\ No newline at end of file",
        ]
    )
    assert scan_patch("config.py", patch) == [
        SecretFinding("aws-access-key-id", "config.py", 12),
        SecretFinding("generic-assignment", "config.py", 42),
    ]


def test_added_lines_handles_crlf_and_missing_hunk_header():
    assert list(added_lines(f"+key = '{AWS}'\r\n+x = 1\r\n")) == [
        (1, f"key = '{AWS}'\r"),
        (2, "x = 1\r"),
    ]


def test_specific_rule_suppresses_generic_duplicate_on_same_line():
    assert matching_rules(f'token = "{GHP}"') == ["github-token"]


def test_scan_files_skips_files_without_patch():
    files = [
        ChangedFile("image.png", "added", None),
        ChangedFile("a.py", "modified", f"@@ -0,0 +1 @@\n+k = '{AWS}'"),
    ]
    assert scan_files(files) == [SecretFinding("aws-access-key-id", "a.py", 1)]


def test_findings_never_contain_the_matched_value():
    finding = scan_patch("a.py", f"@@ -0,0 +1 @@\n+k = '{AWS}'")[0]
    assert AWS not in repr(finding)
    assert not hasattr(finding, "value")
