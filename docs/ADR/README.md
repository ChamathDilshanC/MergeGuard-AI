# Architecture Decision Records

Each ADR records one decision: its context, the choice, the alternatives, and the consequences.

**Statuses:**

- **Accepted (brief):** decided in the owner's project brief; not yet implemented.
- **Proposed:** recommended by this documentation and awaiting the owner's decision.
- **Accepted / Superseded / Deprecated:** standard lifecycle once implemented.

| ADR | Title | Status | Date |
| --- | --- | --- | --- |
| [0001](0001-serverless-webhook-architecture.md) | Serverless webhook architecture on API Gateway HTTP API and Lambda | Accepted (brief) | 2026-10-21 |
| [0002](0002-zero-runtime-dependencies.md) | Zero runtime dependencies | Accepted (brief) | 2026-10-21 |
| [0003](0003-secrets-in-ssm-parameter-store.md) | Secrets in SSM Parameter Store | Accepted (brief) | 2026-10-21 |
| [0004](0004-secret-scan-gate-before-ai.md) | Deterministic secret scan gates AI review | Accepted (brief) | 2026-10-21 |
| [0005](0005-async-webhook-processing.md) | Asynchronous webhook processing | Accepted (implemented M5, not deployed) | 2026-10-21 |
| [0006](0006-github-identity-pat-then-app.md) | GitHub identity: PAT first, then GitHub App | Accepted (implemented M2 and M6, not deployed) | 2026-10-21 |
| [0007](0007-ssm-placeholders-with-write-only-values.md) | Terraform-managed SSM placeholders with write-only values | Accepted | 2026-10-21 |
| [0008](0008-scanner-suppressions-are-visible-and-withheld-from-ai.md) | Scanner suppressions are visible and withheld from the AI | Accepted (implemented M5) | 2026-10-21 |
| [0009](0009-ci-deploy-with-oidc-and-opt-in-remote-state.md) | CI deploy with GitHub OIDC and opt-in remote state | Accepted (written M5, never run) | 2026-10-21 |
| [0010](0010-github-app-jwt-signing-without-dependencies.md) | GitHub App authentication with RS256 signing in the standard library | Accepted (implemented M6, not deployed) | 2026-10-21 |
| [0011](0011-repository-config-from-the-base-commit.md) | Repository configuration is read from the base commit | Accepted (implemented M6, not deployed) | 2026-10-21 |

**New ADRs:** copy the structure of an existing one, take the next number, and add a row here. Never rewrite an accepted ADR; supersede it with a new one.
