# ADR-0008 — Scanner suppressions are visible and withheld from the AI

- **Status:** Accepted — implemented in M5 (T-504); refines [ADR-0004](0004-secret-scan-gate-before-ai.md)
- **Date:** 2026-10-21
- **Related:** PRD FR-019, FR-005, FR-006 · [SECURITY.md](../SECURITY.md) TM-04, TM-15 · [DESIGN_SYSTEM.md](../DESIGN_SYSTEM.md) §5.4

## Context

FR-019 lets a finding be suppressed, either by an inline marker `mergeguard:ignore-secret` on the same line or by a configured path allow-list such as `tests/fixtures/**`. Real repositories need this: test fixtures and documentation hold fake credentials on purpose.

Two things make suppression a security question rather than a convenience:

1. **A suppression is a way past the gate.** The inline marker is written by the pull request author, so anyone who can open a PR can put it next to a real credential.
2. **ADR-0004 promises that a diff with findings never reaches the AI provider.** A suppressed finding is still a flagged line. If the diff goes to Gemini unchanged, the suppression would send exactly the value the scanner found to a third party.

## Decision

1. **Both mechanisms exist, with different owners.**
   - The inline marker is author-controlled, as in other scanners. It suppresses only the finding on its own line.
   - The path allow-list is **operator-controlled**: the Terraform variable `scan_allow_paths`, passed as `MERGEGUARD_SCAN_ALLOW_PATHS`. A pull request cannot change it. Per-repository files that a PR could edit wait for FR-022 (Phase 3), with operator-defined bounds.
2. **Nothing is suppressed silently.** Every suppressed finding is listed in the review (notice `N-SUPPRESSED`) with its rule, file, line and how it was suppressed. Reviewers see what was waved through.
3. **Suppressed lines are withheld from the AI.** Before the diff is bundled for Gemini, each suppressed added line is replaced by a fixed stub. The rest of the diff is still reviewed, and line numbers and hunk structure stay intact. ADR-0004's guarantee therefore holds for every line the scanner flagged.

   **Private keys need more than the flagged line.** The scanner flags only the `-----BEGIN ... PRIVATE KEY-----` header, and the key material follows on lines that no rule flags. When a PEM finding is suppressed, the added lines after the header are withheld too, up to and including the `END` line (or the end of the added block, if there is no `END` line). A context or removed line ends the block. Without this, suppressing a key header would have sent the key itself to the AI.
4. **An unsuppressed finding still means no AI call at all**, exactly as before.
5. **The allow-list fails closed.** A malformed pattern, an absolute path, `..`, or a pattern made only of wildcards (which would switch the scanner off) is a configuration error, not a silent default.
6. The marker text, like a rule ID, is a contract: changing it is a versioned change.

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| Skip the AI whenever anything was suppressed | Simple and strict, but a PR that touches fixtures would never get an AI review, which defeats the purpose of suppressing |
| Send the diff unchanged | Breaks ADR-0004: the flagged value would leave AWS |
| Allow only the operator's allow-list, no inline marker | Safer, but FR-019 asks for the marker, and one-off false positives would need a redeploy |
| Allow the allow-list to live in the repository now | A pull request could then widen its own exemptions; needs the bounds of FR-022 first |

## Consequences

- A real credential marked `mergeguard:ignore-secret` is **not blocked**. It is listed in the review, it is not sent to the AI, and a human sees it. This residual risk is accepted and tracked as TM-15; GitHub push protection and review remain the backstop.
- The commit status stays `success` for a PR whose only findings were suppressed. If the owner wants suppressions to hold a PR for human review, that is a policy change to decide separately.
- Withholding means the AI never sees a suppressed line, so it cannot comment on it.
- Tests cover the marker, the allow-list, the glob rules, withholding, the PEM key block, and the check that the flagged value and the key material are absent from the Gemini request body.
