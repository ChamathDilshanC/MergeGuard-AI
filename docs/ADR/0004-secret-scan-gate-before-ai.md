# ADR-0004 — Deterministic secret scan gates AI review

- **Status:** Accepted (brief), with Proposed refinements — not yet implemented
- **Date:** 2026-10-21
- **Related:** PRD FR-005, FR-006, FR-019 · [SECURITY.md](../SECURITY.md) TM-04 · [AI_SYSTEM.md](../AI_SYSTEM.md) §3

## Context

The brief scans the diff with regular expressions **before** calling Gemini. If anything matches, it posts a security review and stops. This ordering has two effects: credential leaks are caught deterministically, and a diff containing a credential is never sent to a third-party AI service.

## Decision

1. Secret scanning is deterministic (rule-based), runs first, and has no network dependency.
2. Any finding produces the security review (`REQUEST_CHANGES`) and **skips the AI call entirely**.
3. **Proposed refinements:**
   - scan **added lines only** (RB-03);
   - fix and extend the rule set (RB-02; PRD FR-005);
   - mask values in output;
   - give each rule a stable ID;
   - support suppressions in Phase 2 (FR-019).

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| Let the AI detect secrets | Non-deterministic, and sends the secret to a third party |
| Run AI and scanner in parallel | Faster, but violates the "no secrets to AI" property |
| Entropy-based detection only | High false-positive rate; can be added later as an extra rule with its own tests |

## Consequences

- The product guarantees, and tests (ST-05), that diffs with findings never reach Gemini.
- A PR with a finding gets no AI feedback until the secret is removed. This is accepted: the security fix comes first.
- Regex recall is limited (R-09). The scanner complements, and does not replace, dedicated secret-scanning products and GitHub push protection.
