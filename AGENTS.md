# AGENTS.md — Guide for Coding Agents and Contributors

This file tells any automated coding agent, or a human working like one, how to work in this repository. It is vendor-neutral. Where it conflicts with an explicit instruction from the maintainer in the current task, the maintainer's instruction wins.

## 1. Repository purpose

**MergeGuard AI** (repository working name **ZeroOps-PR**) is a serverless GitHub pull-request reviewer for DevSecOps. A signed GitHub webhook reaches API Gateway → Lambda (Python 3.12). The Lambda then:

1. blocks PRs that contain hardcoded credentials;
2. otherwise posts a Gemini-generated code review.

Infrastructure is Terraform. Read [docs/PRD.md](docs/PRD.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing anything.

## 2. Current state

As of 2026-10-21 the repository contains **documentation only**. There is no source code, Terraform, test suite, or CI yet. The first implementation work is milestone M1 in [docs/TASKS.md](docs/TASKS.md). Do not describe features as implemented until code and tests exist.

## 3. Orientation and directory ownership

| Path | Owns | Status |
| --- | --- | --- |
| `docs/` | Product, architecture, security, AI, testing, deployment documentation; ADRs in `docs/ADR/` | Exists |
| `AGENTS.md`, `CONTRIBUTING.md`, `CONTRIBUTORS.md`, `README.md` | Repository-level guides | Exist |
| `src/lambda_function.py` | Lambda entry point (`lambda_function.lambda_handler`) | Planned |
| `src/mergeguard/` | Application modules ([ARCHITECTURE.md](docs/ARCHITECTURE.md) §4.2) | Planned |
| `tests/` | Unit, contract, integration tests, fixtures, AI evaluation set | Planned |
| `terraform/` | All AWS infrastructure | Planned |
| `.github/workflows/` | CI (and later deploy) workflows | Planned |

The maintainer and sole code owner is [@ChamathDilshanC](https://github.com/ChamathDilshanC).

## 4. Source-of-truth order

When sources disagree, use this order and then fix the lower-ranked source in the same change:

1. The maintainer's explicit instructions in the current task.
2. Accepted ADRs in [docs/ADR/](docs/ADR/README.md).
3. [docs/SECURITY.md](docs/SECURITY.md). Security rules override convenience everywhere.
4. Contracts: [docs/API_SPEC.md](docs/API_SPEC.md), [docs/AI_SYSTEM.md](docs/AI_SYSTEM.md) §5, [docs/DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md).
5. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/PRD.md](docs/PRD.md).
6. [docs/TASKS.md](docs/TASKS.md).
7. Existing code and configuration describe what **is**. If code contradicts docs 2–5, raise it rather than silently following either.

The original project brief is summarised in [docs/PRD.md](docs/PRD.md) §2. Its reference code contains known defects ([ARCHITECTURE.md](docs/ARCHITECTURE.md) §16); never copy it verbatim.

## 5. Conventions

### Python (`src/`, `tests/`)

- Target **Python 3.12** (the Lambda runtime), even if your local interpreter is newer.
- **Runtime imports are limited to the standard library and `boto3`/`botocore`** ([ADR-0002](docs/ADR/0002-zero-runtime-dependencies.md)). Dev-only tools go in `requirements-dev.txt`.
- PEP 8 via `ruff`; type hints on public functions; small pure functions for `scanner`, `policy`, `render`, `webhook`.
- Network access only through `mergeguard/http.py`, with explicit timeouts.
- Logging only through `mergeguard/logging_util.py`, with allow-listed fields ([DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md) §7).
- Code, comments, identifiers, and docs are in English.

### Terraform (`terraform/`)

- `snake_case` names; resource names from the brief are kept ([DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md) §8).
- Pin providers; commit `.terraform.lock.hcl`.
- Build IAM ARNs from `aws_caller_identity` and `aws_region` data sources. No `*` for account or region.
- Never manage secret **values** in Terraform.

### Markdown (`docs/`)

- Relative links; tables with header rows; Mermaid for diagrams.
- Label every capability **Verified**, **Planned (brief)**, **Proposed**, **Assumption**, or **Open** ([docs/README.md](docs/README.md#status-legend)).

## 6. Commands

No command below has been verified yet, because no code or tooling exists. Task T-102 makes them real; update this table when it lands.

| Purpose | Command | Status |
| --- | --- | --- |
| Install dev tools | `python -m pip install -r requirements-dev.txt` | Proposed |
| Lint | `ruff check .` | Proposed |
| Format check | `ruff format --check .` | Proposed |
| Tests | `python -m pytest` | Proposed |
| Coverage | `python -m pytest --cov=src --cov-report=term-missing` | Proposed |
| Type check | TBD — decide in T-102 whether to adopt `mypy` | TBD |
| Terraform format | `terraform -chdir=terraform fmt -check -recursive` | Proposed |
| Terraform validate | `terraform -chdir=terraform init -backend=false` then `terraform -chdir=terraform validate` | Proposed |
| Terraform policy scan | `checkov -d terraform` | Proposed |
| Build package | Built by Terraform `archive_file`; no separate build step | Proposed |
| Deploy | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) §5 — **maintainer only** | Proposed |

## 7. Making changes

**Adding a feature:**

1. Find or add its `FR-` entry in [docs/PRD.md](docs/PRD.md) and its task in [docs/TASKS.md](docs/TASKS.md).
2. Change contracts first (API, templates, log events), then the code.
3. Add tests at the layers listed in [docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md) §8.
4. Update the docs (§9).

**Changing infrastructure:**

- Edit Terraform and run `fmt`, `validate`, and `checkov`. Include the relevant `terraform plan` summary in the PR description.
- Never apply (§11).

**Data migrations:** there is no database (stateless by design, [ARCHITECTURE.md](docs/ARCHITECTURE.md) §7). If a change seems to need persistent state, write an ADR first.

**Changing the AI prompt or model:**

1. Bump `PROMPT_VERSION`.
2. Run the evaluation in [docs/AI_SYSTEM.md](docs/AI_SYSTEM.md) §9.
3. Put the aggregate metrics in the PR description.

## 8. Security and secret handling

- Never write real secrets, tokens, keys, or personal data into code, tests, fixtures, docs, commit messages, or PR text. Use variable **names** and placeholders.
- Fake credentials follow [docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md) §4: build token-shaped strings at test time; use `AKIAIOSFODNN7EXAMPLE` for AWS.
- Never disable or bypass webhook signature verification, even in tests.
- Never log request bodies, diffs, AI raw output, signatures, or secret values.
- Never introduce an `APPROVE` review path or auto-merge.
- Never send diff content to the AI provider when the secret scan has findings.
- Read [docs/SECURITY.md](docs/SECURITY.md) before touching `webhook`, `config`, `scanner`, `render`, `logging_util`, or IAM.

## 9. Documentation update policy

Update docs **in the same PR** as the behaviour change:

| Change | Update |
| --- | --- |
| New or changed requirement | [docs/PRD.md](docs/PRD.md) (FR table and traceability) and [docs/TASKS.md](docs/TASKS.md) |
| HTTP behaviour, GitHub or Gemini calls | [docs/API_SPEC.md](docs/API_SPEC.md) |
| Comment text, templates, log events, naming | [docs/DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md) |
| Prompt, model, output schema | [docs/AI_SYSTEM.md](docs/AI_SYSTEM.md) |
| Components, configuration, error handling | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Terraform variables, deploy steps, runbook | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| Threats, IAM, secrets | [docs/SECURITY.md](docs/SECURITY.md) |
| Significant design decision | New ADR in [docs/ADR/](docs/ADR/README.md) |
| A Proposed item becomes real | Change its status label to Verified |

## 10. Dependency and API change rules

- Adding a runtime dependency requires a new ADR that supersedes [ADR-0002](docs/ADR/0002-zero-runtime-dependencies.md). Without one, do not add it.
- Dev dependencies need a one-line justification in the PR and a pinned version.
- Changing the webhook response contract, the review marker `mergeguard:v1`, scanner rule IDs, or the AI output schema is a **contract change**. Version it ([docs/API_SPEC.md](docs/API_SPEC.md) §7) and update the tests.
- Changing Terraform provider major versions needs a plan review and a note in the PR.

## 11. Prohibited operations

Unless the maintainer explicitly asks for it in the current task, do **not**:

- run `terraform apply`, `terraform destroy`, or any command that changes cloud resources;
- create, read, overwrite, or delete SSM parameters, or run `aws ssm get-parameter --with-decryption`;
- create, edit, or delete GitHub webhooks, tokens, apps, repositories, branches on the remote, or branch-protection rules;
- force-push, rewrite published history, delete branches, bypass hooks (`--no-verify`), or change the remote URL;
- commit `.terraform/`, `*.tfstate*`, `tfplan`, `*.zip`, `terraform.tfvars`, `.env*`, or credential files;
- use `git add .` or `git add -A`; stage explicit paths only;
- weaken a security control to make a test pass.

## 12. Definition of done

A change is complete when:

1. It traces to an `FR-`/`T-` ID (or is a documented chore or fix).
2. Lint, format check, tests, coverage gate, and Terraform checks pass locally. Report exactly which ones ran.
3. Docs are updated per §9.
4. No secrets, real credentials, or generated artifacts are staged (`git diff --cached` reviewed).
5. The PR description lists what changed, how it was verified, and any follow-ups.

Never claim a check passed if it was not run.

## 13. Git protocol

- **Branches:** `feat/<topic>`, `fix/<topic>`, `docs/<topic>`, `infra/<topic>`, `chore/<topic>`, created from `main`.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `infra:`, `chore:`), imperative mood, a subject of 72 characters or fewer, and a body explaining *why* when useful.
- **Staging:** explicit paths only; review `git diff --cached` before committing.
- **Push:** a regular `git push origin <branch>`. If it is rejected, stop and report; never force.
- **Pull requests:** small and focused; link the `FR-`/`T-` IDs; include verification evidence.

## 14. Authorship and attribution

- The only project maintainer and contributor identity for new commits, metadata, and docs is **ChamathDilshanC** ([@ChamathDilshanC](https://github.com/ChamathDilshanC)).
- Commits must be authored with Git `user.name` `ChamathDilshanC` and an email verified on that GitHub account. Never guess, invent, or borrow an email; ask the maintainer if it is not configured. Set identity per repository, not globally.
- Do **not** add co-author trailers (`Co-authored-by:` in any capitalisation), "generated by" footers, badges, or any assistant, model, tool, or bot name to commits, PR text, changelogs, file headers, or docs.
- Never rewrite existing commit history or authorship.
- The bot's own product output (review comments that identify themselves as MergeGuard AI and name the Gemini model) is product behaviour, not authorship, and is defined in [docs/DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md).
