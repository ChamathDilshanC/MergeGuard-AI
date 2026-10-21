"""Scanner suppressions (FR-019, T-504, ADR-0008): the inline marker, the
operator's path allow-list, and withholding suppressed lines from the AI."""

import pytest

from mergeguard.models import ChangedFile, SecretFinding, SuppressedFinding
from mergeguard.scanner import (
    INLINE_SUPPRESSION_MARKER,
    WITHHELD_LINE,
    compile_allow_path,
    scan,
    scan_files,
    withhold_suppressed,
)
from support import AWS_EXAMPLE_KEY_ID, fake_github_token, pem_header

AWS = f"key = '{AWS_EXAMPLE_KEY_ID}'"


def changed(filename: str, *added: str) -> ChangedFile:
    body = "\n".join(f"+{line}" for line in added)
    return ChangedFile(filename, "added", f"@@ -0,0 +1,{len(added)} @@\n{body}")


def test_without_suppressions_scan_matches_scan_files():
    files = [changed("a.py", AWS)]
    result = scan(files)
    assert result.findings == scan_files(files)
    assert result.suppressed == []


def test_inline_marker_on_the_same_line_suppresses_the_finding():
    result = scan([changed("a.py", f"{AWS}  # {INLINE_SUPPRESSION_MARKER}")])
    assert result.findings == []
    assert result.suppressed == [
        SuppressedFinding("aws-access-key-id", "a.py", 1, "inline")
    ]


def test_marker_on_another_line_does_not_suppress():
    result = scan([changed("a.py", f"# {INLINE_SUPPRESSION_MARKER}", AWS)])
    assert result.findings == [SecretFinding("aws-access-key-id", "a.py", 2)]
    assert result.suppressed == []


def test_marker_only_suppresses_its_own_line():
    result = scan(
        [changed("a.py", f"{AWS}  # {INLINE_SUPPRESSION_MARKER}", AWS, "x = 1")]
    )
    assert [f.line for f in result.findings] == [2]
    assert [s.line for s in result.suppressed] == [1]


def test_marker_in_a_removed_or_context_line_suppresses_nothing():
    patch = f"@@ -1,2 +1,2 @@\n-# {INLINE_SUPPRESSION_MARKER}\n+{AWS}\n y = 2"
    result = scan([ChangedFile("a.py", "modified", patch)])
    assert len(result.findings) == 1 and result.suppressed == []


def test_path_allow_list_suppresses_every_finding_in_matching_files():
    token = fake_github_token()
    files = [
        changed("tests/fixtures/keys/a.json", AWS, f"t = '{token}'"),
        changed("src/app.py", AWS),
    ]
    result = scan(files, ["tests/fixtures/**"])
    assert [(f.filename, f.line) for f in result.findings] == [("src/app.py", 1)]
    assert {(s.rule_id, s.via) for s in result.suppressed} == {
        ("aws-access-key-id", "path"),
        ("github-token", "path"),
    }


def test_path_wins_over_inline_marker_in_the_listing():
    result = scan(
        [changed("tests/a.py", f"{AWS}  # {INLINE_SUPPRESSION_MARKER}")], ["tests/**"]
    )
    assert [s.via for s in result.suppressed] == ["path"]


def test_files_without_patch_are_skipped():
    assert scan([ChangedFile("logo.png", "added", None)]).findings == []


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("tests/fixtures/**", "tests/fixtures/a.json", True),
        ("tests/fixtures/**", "tests/fixtures/deep/er/a.json", True),
        ("tests/fixtures/**", "tests/other/a.json", False),
        ("tests/fixtures/**", "src/tests/fixtures/a.json", False),
        ("**/*.pem", "keys/server.pem", True),
        ("**/*.pem", "server.pem", True),
        ("**/*.pem", "keys/server.pem.txt", False),
        ("*.md", "README.md", True),
        ("*.md", "docs/README.md", False),  # * stays inside one path segment
        ("docs/?.md", "docs/a.md", True),
        ("docs/?.md", "docs/ab.md", False),
        ("a.b", "aXb", False),  # '.' is literal
        ("a+b(1)", "a+b(1)", True),
    ],
)
def test_path_glob_semantics(pattern, path, expected):
    assert (compile_allow_path(pattern).fullmatch(path) is not None) is expected


def test_withhold_replaces_only_the_suppressed_added_lines():
    patch = "@@ -1,2 +1,4 @@\n ctx\n+first\n+" + AWS + "\n+last"
    files = [ChangedFile("a.py", "modified", patch)]
    suppressed = [SuppressedFinding("aws-access-key-id", "a.py", 3, "inline")]
    [result] = withhold_suppressed(files, suppressed)
    assert result.patch == f"@@ -1,2 +1,4 @@\n ctx\n+first\n{WITHHELD_LINE}\n+last"
    assert AWS_EXAMPLE_KEY_ID not in result.patch
    assert files[0].patch.count(AWS_EXAMPLE_KEY_ID) == 1  # the input is untouched


def test_withhold_keeps_other_files_and_matches_by_file_name():
    other = changed("b.py", AWS)
    untouched = changed("c.py", "x = 1")
    suppressed = [SuppressedFinding("aws-access-key-id", "a.py", 1, "path")]
    result = withhold_suppressed([other, untouched], suppressed)
    assert result == [other, untouched]  # a.py is not in the list: nothing changes


def test_withhold_handles_files_without_patch_and_no_suppressions():
    binary = ChangedFile("logo.png", "added", None)
    suppressed = [SuppressedFinding("aws-access-key-id", "logo.png", 1, "path")]
    assert withhold_suppressed([binary], suppressed) == [binary]
    files = [changed("a.py", AWS)]
    assert withhold_suppressed(files, []) == files


# --- a suppressed PEM header must not leak the key material that follows ------

PEM_END = "-----END RSA " + "PRIVATE " + "KEY-----"
KEY_BODY = ["MIIEowIBAAKCAQEA" + "x" * 48, "yQ9fFakeKeyMaterial" + "z" * 45]


def key_file(*, marker: bool = True) -> ChangedFile:
    header = pem_header("RSA ") + (f"  # {INLINE_SUPPRESSION_MARKER}" if marker else "")
    return changed("keys/dev.pem", "import os", header, *KEY_BODY, PEM_END, "tail = 1")


def withheld_patch(file: ChangedFile, allow_paths=()) -> str:
    result = scan([file], allow_paths)
    [out] = withhold_suppressed([file], result.suppressed)
    return out.patch


def test_a_suppressed_key_header_withholds_the_key_material_after_it():
    patch = withheld_patch(key_file())
    assert pem_header("RSA ") not in patch and PEM_END not in patch
    assert not any(line in patch for line in KEY_BODY)
    assert patch.count(WITHHELD_LINE) == 4  # header, two body lines, END
    assert "+import os" in patch and "+tail = 1" in patch  # the rest is kept


def test_a_path_allow_listed_key_file_withholds_the_key_too():
    patch = withheld_patch(key_file(marker=False), ["keys/**"])
    assert not any(line in patch for line in KEY_BODY)
    assert patch.count(WITHHELD_LINE) == 4


def test_without_an_end_line_the_whole_added_block_after_the_header_is_withheld():
    file = changed(
        "keys/dev.pem",
        pem_header("RSA ") + f" # {INLINE_SUPPRESSION_MARKER}",
        *KEY_BODY,
    )
    assert withheld_patch(file).count(WITHHELD_LINE) == 3


def test_a_context_line_ends_the_withheld_block():
    patch = (
        "@@ -1,3 +1,5 @@\n"
        f"+{pem_header('RSA ')} # {INLINE_SUPPRESSION_MARKER}\n"
        " unchanged context\n"
        "+after = 'kept'\n"
        "+more = 'kept'"
    )
    file = ChangedFile("keys/dev.pem", "modified", patch)
    out = withheld_patch(file)
    assert out.count(WITHHELD_LINE) == 1
    assert "+after = 'kept'" in out and "+more = 'kept'" in out


def test_other_rules_never_withhold_the_lines_after_them():
    file = changed("a.py", f"{AWS}  # {INLINE_SUPPRESSION_MARKER}", "next = 'kept'")
    out = withheld_patch(file)
    assert out.count(WITHHELD_LINE) == 1 and "+next = 'kept'" in out
