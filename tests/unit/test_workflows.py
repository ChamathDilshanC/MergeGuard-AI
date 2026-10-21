"""Static checks of the GitHub Actions workflows (T-505, SECURITY.md TM-10).

These read the YAML as text, because the repository has no YAML dependency
(ADR-0002 covers runtime code; tools stay minimal). They guard the properties
that matter: pinned actions, OIDC instead of AWS keys, and an approval gate
between the plan and the apply."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
WORKFLOWS = sorted(path.name for path in WORKFLOW_DIR.glob("*.yml"))

USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(?:\s+#\s*(\S+))?\s*$", re.MULTILINE)
PINNED_RE = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")


def text(name: str) -> str:
    return (WORKFLOW_DIR / name).read_text(encoding="utf-8")


def job(name: str, job_name: str) -> str:
    """The text of one top-level job."""
    body = text(name)
    start = re.search(rf"^  {job_name}:\n", body, re.MULTILINE)
    assert start, f"{name} has no job {job_name}"
    following = re.search(r"^  [\w-]+:\n", body[start.end() :], re.MULTILINE)
    end = start.end() + following.start() if following else len(body)
    return body[start.start() : end]


def top_level(name: str) -> str:
    """Everything before the ``jobs:`` key."""
    body = text(name)
    return body[: body.index("\njobs:\n")]


def test_the_expected_workflows_exist():
    assert WORKFLOWS == ["ci.yml", "deploy.yml"]


@pytest.mark.parametrize("name", WORKFLOWS)
def test_every_action_is_pinned_to_a_full_commit_sha(name):
    uses = USES_RE.findall(text(name))
    assert uses, f"{name} uses no actions"
    for action, comment in uses:
        assert PINNED_RE.fullmatch(action), f"{name}: {action} is not pinned to a SHA"
        assert re.fullmatch(r"v\d+(\.\d+){1,2}", comment), (
            f"{name}: {action} needs a '# vX.Y.Z' comment"
        )


@pytest.mark.parametrize("name", WORKFLOWS)
def test_the_default_token_is_read_only(name):
    permissions = re.search(
        r"^permissions:\n((?:  .*\n)+)", top_level(name) + "\n", re.MULTILINE
    )
    assert permissions, f"{name} has no top-level permissions"
    assert permissions.group(1).strip() == "contents: read"


def test_ci_never_requests_an_oidc_token():
    assert "id-token" not in text("ci.yml")


# --- deploy.yml: T-505 acceptance criteria -----------------------------------


def test_deploy_is_manual_only():
    """No push trigger until the repository variables exist (see the header)."""
    triggers = top_level("deploy.yml").split("\non:\n")[1].split("\npermissions:")[0]
    assert triggers.strip() == "workflow_dispatch:"


def test_deploy_uses_no_static_aws_credentials():
    body = text("deploy.yml")
    assert not re.search(r"aws[-_]?(access[-_]?key|secret)", body, re.IGNORECASE)
    assert "secrets." not in body  # variables hold only non-secret identifiers
    assert body.count("role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}") == 2


def test_only_the_two_deploy_jobs_may_request_an_oidc_token():
    assert "id-token" not in top_level("deploy.yml")
    for name in ("plan", "apply"):
        assert re.search(
            r"permissions:\n      contents: read\n      id-token: write(?: #.*)?\n",
            job("deploy.yml", name),
        )
    assert text("deploy.yml").count("id-token: write") == 2


def test_apply_waits_for_the_plan_and_for_the_approval_environment():
    plan, apply = job("deploy.yml", "plan"), job("deploy.yml", "apply")
    assert "environment: production-plan" in plan
    assert re.search(r"^    needs: plan$", apply, re.MULTILINE)
    assert re.search(r"^    environment: production(\s|$)", apply, re.MULTILINE)


def test_apply_applies_the_saved_plan_and_never_auto_approves():
    body = text("deploy.yml")
    assert "-auto-approve" not in body
    assert "plan -input=false -lock-timeout=120s -out=tfplan" in job(
        "deploy.yml", "plan"
    )
    assert "apply -input=false -lock-timeout=120s tfplan" in job("deploy.yml", "apply")


def test_the_plan_carries_the_lambda_package_to_the_apply_job():
    """The saved plan refers to terraform/build/lambda.zip, which the apply job
    does not rebuild."""
    plan, apply = job("deploy.yml", "plan"), job("deploy.yml", "apply")
    assert "terraform/tfplan" in plan and "terraform/build/lambda.zip" in plan
    assert "retention-days: 1" in plan
    assert "name: tfplan" in apply and "path: terraform" in apply


def test_only_the_main_branch_can_deploy():
    for name in ("plan", "apply"):
        assert "if: github.ref == 'refs/heads/main'" in job("deploy.yml", name)


def test_deploys_never_run_in_parallel_or_get_cancelled():
    assert "group: deploy\n  cancel-in-progress: false" in top_level("deploy.yml")


def test_state_lives_in_the_remote_bucket_with_the_lock_file():
    for name in ("plan", "apply"):
        body = job("deploy.yml", name)
        assert 'backend "s3" {}' in body
        assert 'backend-config="use_lockfile=true"' in body
        assert 'backend-config="encrypt=true"' in body
        assert "-lockfile=readonly" in body  # the committed lock file is the pin


def test_the_state_key_matches_the_bootstrap_stack():
    key = re.search(r"STATE_KEY: (\S+)", text("deploy.yml")).group(1)
    bootstrap = (ROOT / "terraform" / "bootstrap" / "variables.tf").read_text(
        encoding="utf-8"
    )
    assert f'default     = "{key}"' in bootstrap


def test_the_terraform_version_matches_ci():
    deploy = re.search(r"TF_VERSION: (\S+)", text("deploy.yml")).group(1)
    assert f"terraform_version: {deploy}" in text("ci.yml")


def test_environments_match_the_trust_policy_defaults():
    variables = (ROOT / "terraform" / "bootstrap" / "variables.tf").read_text(
        encoding="utf-8"
    )
    plan_environment = re.search(
        r"environment: (\S+)", job("deploy.yml", "plan")
    ).group(1)
    apply_environment = re.search(
        r"environment: (\S+)", job("deploy.yml", "apply")
    ).group(1)
    assert f'default     = "{plan_environment}"' in variables
    assert f'default     = "{apply_environment}"' in variables


def test_the_generated_backend_file_is_git_ignored():
    assert "*_override.tf" in (ROOT / ".gitignore").read_text(encoding="utf-8")


# --- ci.yml ------------------------------------------------------------------


def test_ci_checks_the_bootstrap_stack_without_credentials():
    body = job("ci.yml", "terraform")
    assert "terraform -chdir=terraform/bootstrap init -backend=false" in body
    assert "terraform -chdir=terraform/bootstrap validate" in body
    assert "terraform -chdir=terraform/bootstrap test" in body
    assert "configure-aws-credentials" not in text("ci.yml")


def test_ci_fails_when_checkov_cannot_parse_a_terraform_file():
    """checkov exits 0 after skipping a file it cannot parse, which would remove
    that file from the scan without a trace."""
    body = job("ci.yml", "terraform")
    assert "checkov -d terraform | tee checkov.log" in body
    assert 'grep -q "Parsing errors" checkov.log' in body
    assert "set -o pipefail" in body
