# ADR-0010 — GitHub App authentication with RS256 signing in the standard library

- **Status:** Accepted — implemented in M6 (T-601), tested with fakes and checked against OpenSSL, **not deployed**; settles the open choice in [ADR-0006](0006-github-identity-pat-then-app.md)
- **Date:** 2026-10-21
- **Related:** PRD FR-020, R-05, R-06 · [SECURITY.md](../SECURITY.md) §5.2, §5.11, TM-18 · [ADR-0002](0002-zero-runtime-dependencies.md)

## Context

A GitHub App authenticates with a JWT signed by its RSA private key (RS256), then exchanges it for a short-lived installation token. ADR-0006 left open how to sign, because the standard library has no RSA and [ADR-0002](0002-zero-runtime-dependencies.md) forbids a runtime dependency without a new ADR.

## Decision

1. **Sign in pure Python** (`src/mergeguard/rsa_jwt.py`): read an unencrypted PKCS#1 or PKCS#8 key, and compute RSASSA-PKCS1-v1_5 over SHA-256 with `pow()`. ADR-0002 stays intact: no new dependency, no layer.
2. **Treat it as hand-written cryptography and harden it:**
   - the private-key operation is **blinded** with a fresh random factor;
   - every signature is **verified with the public exponent** before it is returned, so a faulty computation never leaks a usable signature;
   - the DER reader is strict (definite lengths, no trailing bytes), the key must be 2048 to 8192 bits, encrypted keys are refused, and the private exponent is kept out of `repr`;
   - the code is small, has no other algorithms, and is covered at 100% by tests that generate their own keys.
3. **Verified against a real implementation.** In M6 the signer was checked against OpenSSL 3.5 for PKCS#1 and PKCS#8 keys of 2048 and 3072 bits: OpenSSL accepted every signature and rejected a tampered message. That one-off check is not in the test suite, which must not depend on a system tool.
4. **The key lives in SSM** as a fourth SecureString (`github_app_private_key`, a Terraform placeholder like the others, [ADR-0007](0007-ssm-placeholders-with-write-only-values.md)). Only the worker can read it. In App mode the personal access token is never read.
5. **Installation tokens are scoped.** Each token is minted for **one repository** with only the permissions in use (`pull_requests: write`, plus `contents: read` when repository configuration is on and `statuses: write` when the commit status is on), and cached until five minutes before it expires.
6. **Identity is the App's bot account.** An installation token cannot call `GET /user`, so the duplicate and dismissal checks (TM-14) take the login from the App's slug (`<slug>[bot]`, `GET /app`).

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| AWS KMS asymmetric signing | The key would never leave KMS, but GitHub generates the App key and I could not verify that KMS can import it for signing; adds KMS cost and a key policy |
| `cryptography` in a Lambda layer | Simplest code, but supersedes ADR-0002 and adds a build and a supply-chain surface |
| Keep the personal access token | Remains supported (the default), but keeps the own-PR `422`, a personal identity and manual rotation |

## Consequences

- Reviews appear as `<app-slug>[bot]` and the own-PR limitation no longer applies. The fallback code stays for token mode.
- The signing code is security-sensitive and unaudited by a third party. It is small, tested and cross-checked, but a maintainer who wants an audited library can supersede this ADR with the `cryptography` option.
- Signing is not constant-time in the strict sense (Python big integers), which is why blinding is used; nothing an attacker can observe depends on the signature timing, because tokens are minted inside Lambda.
- The Checks API (named in FR-020's text) is **not** used: the commit status of M5 remains the signal.
- Rotating the App key is an SSM update; a rotated key is picked up without a restart.
