# ADR-0009 — CI deploy with GitHub OIDC and opt-in remote state

- **Status:** Accepted — written in M5 (T-505), **never run**; leaves [OQ-08](../PRD.md#17-open-questions) open
- **Date:** 2026-10-21
- **Related:** [DEPLOYMENT.md](../DEPLOYMENT.md) §8 · [SECURITY.md](../SECURITY.md) TM-09, TM-10, TM-16, §5.10 · [ADR-0007](0007-ssm-placeholders-with-write-only-values.md)

## Context

DEPLOYMENT.md §8 asks for deployment from CI with approval, no long-lived AWS keys in GitHub, and remote Terraform state. Three decisions follow, and one open question must not be settled by accident:

- A CI runner is ephemeral, so a deploy from CI needs **remote state**. [OQ-08](../PRD.md#17-open-questions) still asks whether the demo uses local or remote state, and the first real deploy (T-402) may happen from the maintainer's machine.
- GitHub can show a plan to a reviewer only if the plan exists **before** the approval gate.
- The state bucket cannot be created by the stack whose state it stores.

## Decision

1. **Remote state is opt-in, not the default.** `terraform/versions.tf` keeps no backend block, so `terraform init` still uses local state. The deploy workflow writes `backend_override.tf` (git-ignored) with an empty `backend "s3" {}` block at run time and passes the bucket, key and region as `-backend-config`. The S3 backend uses its native lock file (`use_lockfile`, Terraform 1.11 or later). OQ-08 stays open.
2. **A separate `terraform/bootstrap/` stack** creates the state bucket (versioned, encrypted, private, TLS-only, `prevent_destroy`) and the deploy role. The maintainer applies it once, by hand, with local state. CI never applies it.
3. **GitHub OIDC, no AWS keys.** The deploy role trusts GitHub's OIDC provider for audience `sts.amazonaws.com` and for exactly two subjects, both for this repository: the `production-plan` and `production` environments. The workflow stores only non-secret identifiers (role ARN, region, bucket) as repository **variables**.
4. **Two jobs, one approval.** `plan` runs in `production-plan` (no reviewers), prints the plan to the job summary and uploads the saved plan with the built Lambda zip. `apply` needs `plan`, runs in `production` (required reviewers), and applies **exactly that saved plan**. GitHub does not release the OIDC token of an environment job until its reviewers approve, so AWS access to apply follows the approval.
5. **Manual trigger only.** `workflow_dispatch`, and only on `main`. A `push` trigger would fail on every push until the variables exist, and nothing is deployed yet.
6. **The deploy role is scoped, not administrator.** Write actions are limited to `mergeguard*` resources in one account and region, and `iam:PassRole` is limited to Lambda. It is a starting point for the first real deploy to confirm.

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| Static AWS access keys in GitHub secrets | Long-lived credentials in a third party; the brief and [SECURITY.md](../SECURITY.md) rule them out |
| Make S3 the default backend in `versions.tf` | Settles OQ-08, and every local `init` would then need bucket settings |
| One job with the approval first | The approver would approve a commit, not a plan |
| Two roles (read-only plan, full apply) | Tighter, but the plan role must read every resource type and the SSM parameters; unverifiable without AWS, so deferred (residual risk in TM-16) |
| Create the bucket from the main stack | Chicken and egg: the stack cannot store its state in a bucket it has not created |
| Trigger on every push to `main` | Fails noisily until configured; revisit after T-402 |

## Consequences

- **Moving from local to remote state is a manual step** (`terraform init -migrate-state` with the same `-backend-config` values). If the first deploy runs from a laptop with local state, migrate before the first CI deploy, or CI will plan to create everything again.
- The plan job holds a role that can also apply. Anyone who can change the workflow on `main` could bypass the approval, so `main` needs branch protection and both environments need a main-only deployment branch rule. This is TM-16.
- The deploy role can read the real SSM values through the AWS provider's refresh, because the provider reads each managed parameter. They stay out of state and logs (ADR-0007) but not out of the runner's memory. Recorded in [SECURITY.md](../SECURITY.md) §5.10.
- Nothing here has run. The workflow is checked with `actionlint` and static tests, and the bootstrap stack with credential-free plan tests. The first real run is part of T-402.
