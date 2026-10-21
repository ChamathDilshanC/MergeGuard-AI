# ADR-0002 — Zero runtime dependencies

- **Status:** Accepted (brief) — not yet implemented
- **Date:** 2026-10-21
- **Related:** [ARCHITECTURE.md](../ARCHITECTURE.md) §3, §4 · PRD NFR-008

## Context

The brief explicitly chose to use only modules available in the AWS Lambda Python runtime: the standard library (`json`, `hmac`, `hashlib`, `re`, `urllib.request`) and the bundled `boto3`. This avoids `pip install` steps and Lambda layers, so Terraform can zip and deploy the source directly.

## Decision

Code under `src/` may import only the Python standard library and `boto3`/`botocore` as provided by the Lambda runtime. Development and test tools (`pytest`, `ruff`, `checkov`) are allowed as **dev-only** dependencies and are never packaged.

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| `requests` / `httpx` for HTTP | Nicer API, but needs packaging; `urllib` is sufficient for a handful of JSON calls |
| Google's Gemini SDK, PyGithub | Large dependency trees and a bigger supply-chain surface for a few endpoints |
| Lambda layer with dependencies | Adds a build and versioning pipeline; contradicts the zero-ops goal |

## Consequences

- Fast cold starts, a tiny package, almost no supply-chain risk, and deployment with `archive_file` alone.
- A small in-house HTTP helper with timeouts and error mapping is required ([ARCHITECTURE.md](../ARCHITECTURE.md) §5, `http.py`).
- Configuration files must be JSON, not YAML (FR-022).
- GitHub App authentication needs RS256 JWT signing, which the standard library does not provide. This must be resolved in [ADR-0006](0006-github-identity-pat-then-app.md) without silently breaking this decision.
- Adding any runtime dependency requires a new ADR that supersedes this one.
