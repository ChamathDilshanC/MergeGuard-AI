# Contributing to MergeGuard AI

Thank you for your interest. This project is maintained by [@ChamathDilshanC](https://github.com/ChamathDilshanC), the sole listed maintainer, who reviews and merges all changes.

> **Project status:** the M1 foundation (tooling, Terraform skeleton, CI) is in place; no product feature is implemented yet. Feature work starts with milestone M2 in [docs/TASKS.md](docs/TASKS.md).

## 1. Before you start

- Read [README.md](README.md), [docs/PRD.md](docs/PRD.md), and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- Read [AGENTS.md](AGENTS.md). Its conventions, prohibited operations, and definition of done apply to human contributors too.
- Changes touching security-sensitive modules (`webhook`, `config`, `scanner`, `render`, `logging_util`, IAM) must follow [docs/SECURITY.md](docs/SECURITY.md).

## 2. Prerequisites

| Tool | Version | Needed for |
| --- | --- | --- |
| Git | Recent | Everything |
| Python | **3.12** (Lambda runtime) | Code and tests |
| Terraform | `>= 1.5, < 2.0` (CI uses 1.16.5) | Infrastructure changes |
| AWS CLI | v2 recommended | Deployment by the maintainer only |

You do **not** need AWS, GitHub, or Gemini credentials to develop or run the test suite; tests run offline ([docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md)).

## 3. Setup

```bash
git clone <repository-url>
cd <repository-folder>
python3.12 -m venv .venv           # Windows: py -3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
ruff check . && ruff format --check .
python -m pytest --cov=src --cov-report=term-missing
```

Terraform checks are listed in [AGENTS.md](AGENTS.md) §6.

## 4. Workflow

1. Open or pick an issue, and link it to an `FR-` or `T-` ID where one exists.
2. Create a branch from `main`: `feat/…`, `fix/…`, `docs/…`, `infra/…`, or `chore/…`.
3. Make small, focused commits using [Conventional Commits](https://www.conventionalcommits.org/) (`feat: add secret scanner rule for GitHub tokens`).
4. Stage files explicitly (`git add <path>`), not `git add .`, and review `git diff --cached`.
5. Push your branch and open a pull request against `main`.

## 5. Code style

- Python: PEP 8 enforced by `ruff`; type hints on public functions; English names and comments.
- **Runtime code may use only the Python standard library and `boto3`** ([ADR-0002](docs/ADR/0002-zero-runtime-dependencies.md)).
- Terraform: `terraform fmt`; `snake_case`; pinned providers; no secret values.
- Markdown: relative links; status labels per [docs/README.md](docs/README.md#status-legend).

## 6. Testing requirements

- Add or update tests for every behaviour change, at the layers listed in [docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md) §8.
- Scanner changes need positive **and** negative corpus cases.
- Prompt or model changes need an AI evaluation summary ([docs/AI_SYSTEM.md](docs/AI_SYSTEM.md) §9).
- CI quality gates: [docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md) §9.

## 7. Pull request checklist

- [ ] Linked to an issue or an `FR-`/`T-` ID.
- [ ] Lint, format, tests, and coverage pass locally; Terraform checks pass if `terraform/` changed.
- [ ] Docs updated per [AGENTS.md](AGENTS.md) §9.
- [ ] No secrets, real credentials, state files, plan files, or build artifacts included.
- [ ] No new runtime dependency (or an ADR is included).
- [ ] Security-relevant changes explained in the description.
- [ ] The description says what changed, why, and how it was verified.

## 8. Reporting issues

For bugs, open an issue with:

- expected vs. actual behaviour;
- steps to reproduce;
- the webhook delivery ID and the relevant **redacted** log lines, if available;
- the version or commit.

Never paste tokens, secrets, private code, or unredacted logs.

**Security vulnerabilities:** do **not** open a public issue. Follow [docs/SECURITY.md](docs/SECURITY.md) §9.

## 9. Documentation and changelog

- Documentation is part of the change. A PR that changes behaviour without updating docs is incomplete.
- A `CHANGELOG.md` ([Keep a Changelog](https://keepachangelog.com/) format) will be introduced with the first tagged release (Proposed); add an entry under *Unreleased* once it exists.

## 10. Protecting sensitive data

- Use placeholders like `<github-token>` in docs and examples.
- Use only the fake values allowed in [docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md) §4 in tests.
- Never include real repository names, personal data, or credentials in fixtures, and never commit `.env` files or Terraform state.

## 11. Review and merge

The maintainer reviews every pull request. Expect feedback on scope, tests, security, and docs. Squash-merge is the proposed default, so write the PR title as a valid Conventional Commit subject.
