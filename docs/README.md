# MergeGuard AI — Documentation

Documentation for MergeGuard AI, a serverless DevSecOps pull-request reviewer. For a project overview, start with the [root README](../README.md).

## Status legend

| Label | Meaning |
| --- | --- |
| **Verified** | Implemented and confirmed in this repository (code, configuration, or a recorded test run) |
| **Planned (brief)** | Specified in the owner's project brief; not yet implemented |
| **Proposed** | Recommended by these documents; needs the owner's approval before it counts as decided |
| **Assumption** | Treated as true for planning; must be validated |
| **Open** / **TBD** | Undecided; tracked as `OQ-` items in [PRD.md](PRD.md) §17 |

As of 2026-10-21, M1 to M6 are Verified by automated tests: Gemini is mocked, SQS and GitHub are faked, and the Terraform plans are tested against a mocked AWS provider. **Nothing is deployed**, the CI deploy workflow has never run, and no real Gemini call has been made (the paid tier is decided in OQ-05, 2026-10-22, but no key is stored). The decisions on language, Gemini tier and budget, bot identity, state backend and region are recorded in [PRD.md](PRD.md) §17. Properties that need AWS (acknowledgement latency, alarms firing, the deploy itself) are Planned until T-402 and T-403.

## Core documents

| Document | Purpose |
| --- | --- |
| [PRD.md](PRD.md) | Problem, users, goals, functional requirements (`FR-`), non-functional requirements, risks, open questions, roadmap, traceability |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System context, components, request lifecycle, configuration, error handling, target architecture, review of the brief's reference code (`RB-`) |
| [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) | Headless "UI": PR review comment templates, verdict tokens, accessibility, HTTP and log conventions, naming |
| [TASKS.md](TASKS.md) | Phased engineering backlog (`T-`) with acceptance criteria and status |

## Specialised documents

| Document | Purpose |
| --- | --- |
| [API_SPEC.md](API_SPEC.md) | Inbound webhook contract; outbound GitHub, Gemini, SSM and SQS calls; error mapping; queue message |
| [SECURITY.md](SECURITY.md) | Assets, trust boundaries, threat model (`TM-`), controls, secret rotation, incident runbooks, vulnerability reporting |
| [AI_SYSTEM.md](AI_SYSTEM.md) | Role of AI, data handling, prompt architecture, output schema, failure handling, evaluation, model lifecycle |
| [TESTING_STRATEGY.md](TESTING_STRATEGY.md) | Test layers, fixtures, scanner corpus, security tests, E2E scenarios, quality gates |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Secrets setup, Terraform deployment, webhook configuration, CI/CD plan, rollback, runbook, teardown |

## Architecture decision records

| ADR | Title | Status |
| --- | --- | --- |
| [0001](ADR/0001-serverless-webhook-architecture.md) | Serverless webhook architecture | Accepted (brief) |
| [0002](ADR/0002-zero-runtime-dependencies.md) | Zero runtime dependencies | Accepted (brief) |
| [0003](ADR/0003-secrets-in-ssm-parameter-store.md) | Secrets in SSM Parameter Store | Accepted (brief) |
| [0004](ADR/0004-secret-scan-gate-before-ai.md) | Secret scan gates AI review | Accepted (brief) |
| [0005](ADR/0005-async-webhook-processing.md) | Asynchronous webhook processing | Accepted (implemented M5, not deployed) |
| [0006](ADR/0006-github-identity-pat-then-app.md) | GitHub identity: PAT, then GitHub App | Accepted (implemented M2 and M6, not deployed) |
| [0007](ADR/0007-ssm-placeholders-with-write-only-values.md) | SSM placeholders with write-only values | Accepted |
| [0008](ADR/0008-scanner-suppressions-are-visible-and-withheld-from-ai.md) | Scanner suppressions are visible and withheld from the AI | Accepted (implemented M5) |
| [0009](ADR/0009-ci-deploy-with-oidc-and-opt-in-remote-state.md) | CI deploy with GitHub OIDC and opt-in remote state | Accepted (written M5, never run) |
| [0010](ADR/0010-github-app-jwt-signing-without-dependencies.md) | GitHub App authentication with RS256 signing in the standard library | Accepted (implemented M6, not deployed) |
| [0011](ADR/0011-repository-config-from-the-base-commit.md) | Repository configuration is read from the base commit | Accepted (implemented M6, not deployed) |

See the [ADR index](ADR/README.md) for the process.

## Repository guides

| Document | Purpose |
| --- | --- |
| [AGENTS.md](../AGENTS.md) | Operating guide for coding agents and contributors: conventions, commands, prohibited operations, Git protocol |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Setup, workflow, code style, PR checklist, issue reporting |
| [CONTRIBUTORS.md](../CONTRIBUTORS.md) | Maintainer and contributor record |

## ID conventions

| Prefix | Meaning | Defined in |
| --- | --- | --- |
| `FR-` / `NFR-` | Functional / non-functional requirement | [PRD.md](PRD.md) |
| `G1`–`G5`, `SM-`, `R-`, `A-`, `OQ-`, `EC-`, `P1`–`P4` | Goal, success metric, risk, assumption, open question, edge case, persona | [PRD.md](PRD.md) |
| `RB-` | Defect in the brief's reference implementation | [ARCHITECTURE.md](ARCHITECTURE.md) §16 |
| `TM-`, `IR-` | Threat, incident runbook | [SECURITY.md](SECURITY.md) |
| `ST-`, `E2E-` | Security test, end-to-end scenario | [TESTING_STRATEGY.md](TESTING_STRATEGY.md) |
| `T-` | Engineering task | [TASKS.md](TASKS.md) |
| `T-SEC`, `T-AI`, `T-AIU`, `T-FALLBACK`, `N-` | Comment templates and notices | [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) §5 |
