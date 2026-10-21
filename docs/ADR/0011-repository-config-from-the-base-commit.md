# ADR-0011 — Repository configuration is read from the base commit

- **Status:** Accepted — implemented in M6 (T-603), tested with fakes, **not deployed**
- **Date:** 2026-10-21
- **Related:** PRD FR-022, FR-021 · [SECURITY.md](../SECURITY.md) TM-19 · [ADR-0008](0008-scanner-suppressions-are-visible-and-withheld-from-ai.md), [ADR-0002](0002-zero-runtime-dependencies.md)

## Context

FR-022 lets a repository tune MergeGuard with a JSON file, `.github/mergeguard.json`. A pull request can edit any file in its own branch, including that one. If the review honoured the head's copy, a PR could switch off the AI, ignore its own files or relax its own blocking level. [ADR-0008](0008-scanner-suppressions-are-visible-and-withheld-from-ai.md) deferred repository-held settings to this work for exactly that reason.

## Decision

1. **Read the file from the pull request's base commit** (`base.sha` of `GET /pulls/{n}`), never the head. Whoever can change it needs write access to the base branch.
2. **The operator sets the bounds**; the file can only choose inside them:
   - `block_on`: one of the levels the operator allows (`repo_config_block_levels`, default `MEDIUM,HIGH`). AI bugs and secret findings always block.
   - `inline_comments`: `off`, `secrets` or `all`, never above the operator's mode.
   - `ai_review: false` and `ignore_paths`: keep files from the AI. These only reduce what leaves AWS.
   - `language`: only `en` exists until the language question (OQ-03) is decided.
3. **The file can never touch security controls.** The secret scan always covers every file; the file cannot add suppressions (the allow-list stays operator-only, ADR-0008); it cannot approve, change identity, or reach secrets. An unknown key is an error, so a typo or an attempt such as `scan_allow_paths` is reported, not ignored silently.
4. **Strict, all or nothing.** A file that is not valid JSON, is over 16 KB (or above GitHub's own 1 MB content limit), has an unknown key, an unsupported `version`, a wrong type, or a value outside the bounds is **ignored as a whole**. The defaults apply, and the review carries a notice with a reason category (`invalid_json`, `too_large`, `unknown_key`, `unsupported_version`, `invalid_field`, `out_of_bounds`), never file content. A file that is applied is listed in the review too.
5. **No file, no problem.** A missing file, or a token that cannot read contents (`403`/`404`), means the defaults. A server error is retried like any other GitHub failure.
6. JSON, not YAML, to keep ADR-0002.

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| Read the head's copy | A PR could change the rules that judge it |
| Operator-only configuration | Safe, but a multi-repository deployment needs per-repository choices without a redeploy |
| Merge the valid parts of a broken file | A half-read file is surprising; all or nothing is predictable |
| Let the file suppress findings | Reopens the hole ADR-0008 closed |

## Consequences

- A configuration change takes effect once it is merged into the base branch, not on the PR that proposes it. That is the intended trade-off.
- Reading it costs one extra `GET /pulls/{n}` call in inline mode (the worker already makes it) and one contents call, and needs the token or App permission *Contents: read*.
- `ignore_paths` can reduce AI coverage; the review says that a repository configuration was applied.
