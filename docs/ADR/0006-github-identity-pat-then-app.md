# ADR-0006 — GitHub identity: PAT first, then GitHub App

- **Status:** Accepted — Phase 1 (token) implemented in M2; Phase 3 (App) implemented in M6 under [ADR-0010](0010-github-app-jwt-signing-without-dependencies.md), not deployed
- **Date:** 2026-10-21
- **Related:** PRD FR-010, FR-020, R-05, R-06, OQ-06 · [SECURITY.md](../SECURITY.md) §5.2

## Context

The brief authenticates to GitHub with a fine-grained personal access token (PAT). That works for a quick start, but it has three limitations:

- **Own-PR restriction.** GitHub refuses `REQUEST_CHANGES` (and `APPROVE`) on a PR authored by the token owner. In the brief's demo, where the maintainer opens a PR with their own token, the review fails with `422`.
- **Personal identity.** Reviews appear under a person's account. Expiry and revocation depend on that person.
- **No Checks API.** Check runs require a GitHub App; a PAT can only set commit statuses.

## Decision

1. **Phase 1:** use a fine-grained PAT with minimal permissions, preferably owned by a dedicated machine account. Implement the `422` → `COMMENT` fallback (FR-010).
2. **Phase 3:** move to a **GitHub App**:
   - installation access tokens (short-lived);
   - reviews shown as `<app-name>[bot]`;
   - Checks API available;
   - multi-repository installation (FR-020).
3. Before Phase 3, choose how to sign the App's RS256 JWT without breaking [ADR-0002](0002-zero-runtime-dependencies.md):

   | Option | Notes |
   | --- | --- |
   | Pure-Python RSA PKCS#1 v1.5 signing with the standard library | Feasible (big-integer modular exponentiation plus DER parsing), but security-sensitive code that must be thoroughly tested |
   | AWS KMS asymmetric signing key | The private key never leaves KMS; requires KMS support for importing the App's key material, or another way to provision it — verify before choosing |
   | Supersede ADR-0002 for one dependency (`cryptography`) via a Lambda layer | Simplest code; gives up the zero-dependency property |

## Consequences

- Phase 1 stays simple, at the cost of the fallback behaviour and manual token rotation.
- Phase 3 needed a new ADR that settles the JWT signing choice above and the App permission set: [ADR-0010](0010-github-app-jwt-signing-without-dependencies.md) chose pure-Python RS256 with the permission set `pull_requests: write`, `contents: read` and `statuses: write`. The personal access token remains the default when no App ID is set.

## Update (2026-10-22)

[OQ-06](../PRD.md#17-open-questions) is resolved: the GitHub App is the target identity, **staged**. It is created and proven on a throwaway repository first, and the personal access token stays configured as a working fallback (preferably a dedicated machine account) until the App is confirmed. This confirms the order of this ADR (token first, App later) and does not change its decision.