# ADR-0007 — Terraform-managed SSM placeholders with write-only values

- **Status:** Accepted — requested by the maintainer for M4 (T-401); amends the second decision bullet of [ADR-0003](0003-secrets-in-ssm-parameter-store.md)
- **Date:** 2026-10-21
- **Related:** PRD FR-011, FR-012 · [SECURITY.md](../SECURITY.md) §5.4, TM-09 · [DEPLOYMENT.md](../DEPLOYMENT.md) §3

## Context

ADR-0003 created the three secrets outside Terraform, so that secret values could never reach Terraform state. M4 asks for the SSM parameters to exist as Terraform-managed placeholders, still with no real secret value in code, state or tfvars.

With a normal `aws_ssm_parameter` resource, Terraform reads the decrypted value back into state on every refresh. So after an operator set the real value with the AWS CLI, it would end up in state anyway.

## Decision

1. **Terraform creates the parameters** `<prefix>/github_token`, `<prefix>/webhook_secret` and `<prefix>/gemini_api_key` as `SecureString`. Each gets a fixed, public placeholder written through the **write-only** argument `value_wo` (`value_wo_version = 1`).
   - Write-only values are never stored in plan or state.
   - In AWS provider 5.100.0, every refresh of a parameter with `value_wo` sets `value` to null, which was checked in the provider source.
   - Terraform re-sends the placeholder only if `value_wo_version` changes.
2. **The operator sets the real values** after the first apply with `aws ssm put-parameter --overwrite` ([DEPLOYMENT.md](../DEPLOYMENT.md) §3), as before. Terraform never reads them back into state.
3. **The Lambda treats any value starting with `mergeguard-placeholder` as not set:**
   - placeholder core secrets → fail closed (`500`, `config.failed`);
   - placeholder Gemini key → AI review unavailable (`not_configured`), and **no diff is sent to Gemini**.

   Without this rule, the public placeholder would work as a known webhook secret.
4. **Terraform ≥ 1.11** is required, the first release with write-only arguments.

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| Keep parameters fully outside Terraform (ADR-0003 as written) | Meets the state rule, but not the M4 request for placeholders; IAM and names would not be tied to managed resources |
| `aws_ssm_parameter` with `value` and `lifecycle { ignore_changes = [value] }` | Terraform still reads the real value into state on refresh |
| `terraform import` of parameters created by hand | The import reads the real value into state and state backups |

## Consequences

- Parameter names, type and lifetime are managed and visible in plans. `terraform destroy` now deletes the parameters too.
- Real values never enter state, even after rotation, as long as nobody runs `terraform import` on these parameters. DEPLOYMENT.md says not to.
- Until the operator sets real values, the stack is deployed but inert: every webhook gets `500`, and no data leaves AWS.
- The placeholder string is a contract between `terraform/main.tf` and `src/mergeguard/config.py`, and `tests/unit/test_infra_contract.py` checks it.
