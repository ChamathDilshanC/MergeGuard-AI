# MergeGuard AI — Serverless DevSecOps PR Reviewer

> **Status: in development (M3).** The webhook pipeline (signature check, secret scanner, review posting) and the Gemini AI review are implemented and tested, but **not deployed**. The Gemini integration has only been tested against mocked responses. AWS infrastructure (M4) is not built yet. See the [roadmap](docs/PRD.md#18-roadmap) and [backlog](docs/TASKS.md).

MergeGuard AI reviews GitHub pull requests automatically, without any server to run:

1. GitHub sends a signed `pull_request` webhook to an **Amazon API Gateway HTTP API**.
2. An **AWS Lambda** function (Python 3.12, no third-party runtime packages) verifies the HMAC-SHA256 signature and fetches the PR's changed files.
3. A deterministic **secret scanner** checks the added lines. If it finds credentials, the PR gets a *changes requested* review, and the code is **not** sent to any AI service.
4. Otherwise **Google Gemini** produces a structured review (risk level, bug count, feedback), which MergeGuard posts on the PR. MergeGuard never approves or merges; a human always decides.
5. Secrets live in **AWS SSM Parameter Store**. All infrastructure is defined in **Terraform**.

```mermaid
flowchart LR
    gh["GitHub PR event"] --> api["API Gateway<br/>POST /webhook"]
    api --> fn["Lambda<br/>verify, scan, review"]
    fn --> ssm["SSM Parameter Store"]
    fn --> gem["Gemini API<br/>only if scan is clean"]
    fn --> ghapi["GitHub REST API<br/>post review"]
```

## Documentation

| Start here | |
| --- | --- |
| [Documentation index](docs/README.md) | All documents and the status legend |
| [Product requirements](docs/PRD.md) | What MergeGuard does and why |
| [Architecture](docs/ARCHITECTURE.md) | How it is built, including the review of the reference code |
| [Security](docs/SECURITY.md) | Threat model and controls |
| [Deployment](docs/DEPLOYMENT.md) | How to deploy and operate it |
| [AGENTS.md](AGENTS.md) / [CONTRIBUTING.md](CONTRIBUTING.md) | How to work in this repository |

## Maintainer

[@ChamathDilshanC](https://github.com/ChamathDilshanC). See [CONTRIBUTORS.md](CONTRIBUTORS.md).

## License

No licence has been chosen yet ([OQ-11](docs/PRD.md#17-open-questions)). Until one is added, all rights are reserved by the maintainer.
