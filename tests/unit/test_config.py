"""Settings from the environment and the cached SSM secret store (FR-011)."""

import pytest

from mergeguard.config import ConfigError, SecretStore, Settings
from support import TEST_GITHUB_TOKEN, TEST_WEBHOOK_SECRET, FakeSsm


def test_settings_defaults():
    assert Settings.from_env({}) == Settings("/mergeguard", 300, "INFO", False)


def test_settings_overrides():
    env = {
        "MERGEGUARD_PARAM_PREFIX": "/team/mergeguard",
        "MERGEGUARD_MAX_FILES": "50",
        "MERGEGUARD_LOG_LEVEL": "debug",
        "MERGEGUARD_DRY_RUN": "TRUE",
    }
    assert Settings.from_env(env) == Settings("/team/mergeguard", 50, "DEBUG", True)


@pytest.mark.parametrize(
    "env",
    [
        {"MERGEGUARD_PARAM_PREFIX": "mergeguard"},
        {"MERGEGUARD_PARAM_PREFIX": "/mergeguard/"},
        {"MERGEGUARD_PARAM_PREFIX": "/"},
        {"MERGEGUARD_MAX_FILES": "many"},
        {"MERGEGUARD_MAX_FILES": "0"},
        {"MERGEGUARD_MAX_FILES": "3001"},
        {"MERGEGUARD_LOG_LEVEL": "TRACE"},
        {"MERGEGUARD_DRY_RUN": "maybe"},
    ],
)
def test_invalid_settings_fail_closed(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


@pytest.mark.parametrize(
    ("raw", "expected"), [("1", True), ("yes", True), ("0", False), ("no", False)]
)
def test_dry_run_values(raw, expected):
    assert Settings.from_env({"MERGEGUARD_DRY_RUN": raw}).dry_run is expected


def test_secrets_load_with_one_decrypting_call():
    ssm = FakeSsm()
    secrets = SecretStore("/mergeguard", lambda: ssm).get()
    assert (secrets.github_token, secrets.webhook_secret) == (
        TEST_GITHUB_TOKEN,
        TEST_WEBHOOK_SECRET,
    )
    assert ssm.calls == [
        {
            "Names": ["/mergeguard/github_token", "/mergeguard/webhook_secret"],
            "WithDecryption": True,
        }
    ]


def test_secrets_are_cached_until_the_ttl_expires():
    ssm, now = FakeSsm(), [0.0]
    created = []

    def factory():
        created.append(1)
        return ssm

    store = SecretStore("/mergeguard", factory, ttl_seconds=900, clock=lambda: now[0])
    store.get()
    now[0] = 899.0
    store.get()
    assert len(ssm.calls) == 1
    now[0] = 900.0
    store.get()
    assert len(ssm.calls) == 2
    assert len(created) == 1  # the client is created once


def test_missing_parameter_fails_closed_without_revealing_values():
    ssm = FakeSsm({"/mergeguard/webhook_secret": TEST_WEBHOOK_SECRET})
    with pytest.raises(ConfigError) as caught:
        SecretStore("/mergeguard", lambda: ssm).get()
    assert "/mergeguard/github_token" in str(caught.value)
    assert TEST_WEBHOOK_SECRET not in str(caught.value)


def test_empty_parameter_fails_closed():
    ssm = FakeSsm({"/mergeguard/github_token": "", "/mergeguard/webhook_secret": "x"})
    with pytest.raises(ConfigError):
        SecretStore("/mergeguard", lambda: ssm).get()


def test_ssm_errors_fail_closed_and_are_retried_next_time():
    ssm = FakeSsm(error=RuntimeError("throttled"))
    store = SecretStore("/mergeguard", lambda: ssm)
    with pytest.raises(ConfigError):
        store.get()
    ssm.error = None
    assert store.get().github_token == TEST_GITHUB_TOKEN


def test_secret_values_are_not_in_repr():
    secrets = SecretStore("/mergeguard", FakeSsm).get()
    assert TEST_GITHUB_TOKEN not in repr(secrets)
    assert TEST_WEBHOOK_SECRET not in repr(secrets)
