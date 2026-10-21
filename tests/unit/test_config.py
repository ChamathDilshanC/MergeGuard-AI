"""Settings from the environment and the cached SSM secret store (FR-011)."""

import pytest

from mergeguard.config import PLACEHOLDER_PREFIX, ConfigError, SecretStore, Settings
from support import TEST_GEMINI_KEY, TEST_GITHUB_TOKEN, TEST_WEBHOOK_SECRET, FakeSsm


def test_settings_defaults():
    settings = Settings.from_env({})
    assert settings == Settings("/mergeguard", 300, "INFO", False)
    assert (settings.gemini_model, settings.max_diff_chars) == (
        "gemini-2.5-flash",
        12000,
    )


def test_ai_settings_overrides():
    env = {
        "MERGEGUARD_GEMINI_MODEL": "gemini-2.5-pro",
        "MERGEGUARD_MAX_DIFF_CHARS": "4000",
    }
    settings = Settings.from_env(env)
    assert (settings.gemini_model, settings.max_diff_chars) == ("gemini-2.5-pro", 4000)


@pytest.mark.parametrize(
    "env",
    [
        {"MERGEGUARD_GEMINI_MODEL": "../tunedModels/x"},  # would change the URL path
        {"MERGEGUARD_GEMINI_MODEL": "gemini-2.5-flash:generateContent?key=x"},
        {"MERGEGUARD_GEMINI_MODEL": "Gemini"},
        {"MERGEGUARD_GEMINI_MODEL": ""},
        {"MERGEGUARD_MAX_DIFF_CHARS": "0"},
        {"MERGEGUARD_MAX_DIFF_CHARS": "200001"},
        {"MERGEGUARD_MAX_DIFF_CHARS": "lots"},
    ],
)
def test_invalid_ai_settings_fail_closed(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


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


def test_gemini_key_is_loaded_separately_and_only_on_request():
    ssm = FakeSsm()
    store = SecretStore("/mergeguard", lambda: ssm)
    store.get()
    assert ssm.calls[0]["Names"] == [
        "/mergeguard/github_token",
        "/mergeguard/webhook_secret",
    ]
    assert store.gemini_api_key() == TEST_GEMINI_KEY
    assert store.gemini_api_key() == TEST_GEMINI_KEY  # cached
    assert [call["Names"] for call in ssm.calls[1:]] == [["/mergeguard/gemini_api_key"]]
    assert ssm.calls[1]["WithDecryption"] is True


def test_missing_gemini_key_fails_only_the_key_lookup():
    ssm = FakeSsm(
        {
            "/mergeguard/github_token": TEST_GITHUB_TOKEN,
            "/mergeguard/webhook_secret": TEST_WEBHOOK_SECRET,
        }
    )
    store = SecretStore("/mergeguard", lambda: ssm)
    assert store.get().github_token == TEST_GITHUB_TOKEN  # the secret gate still works
    with pytest.raises(ConfigError):
        store.gemini_api_key()


def test_terraform_placeholders_count_as_not_set():
    """ADR-0007: before the operator sets real values, everything fails safe."""
    placeholder = PLACEHOLDER_PREFIX + ":set-with-aws-ssm-put-parameter"
    ssm = FakeSsm(
        {
            "/mergeguard/github_token": placeholder,
            "/mergeguard/webhook_secret": placeholder,
            "/mergeguard/gemini_api_key": placeholder,
        }
    )
    store = SecretStore("/mergeguard", lambda: ssm)
    with pytest.raises(ConfigError, match="placeholder"):
        store.get()
    with pytest.raises(ConfigError, match="placeholder"):
        store.gemini_api_key()


def test_secret_values_are_not_in_repr():
    secrets = SecretStore("/mergeguard", FakeSsm).get()
    assert TEST_GITHUB_TOKEN not in repr(secrets)
    assert TEST_WEBHOOK_SECRET not in repr(secrets)


# --- Phase 2 settings and per-secret accessors (M5) -----------------------------

QUEUE = "https://sqs.us-east-1.amazonaws.com/123456789012/mergeguard-review-requests"


def test_phase_2_settings_default_to_phase_1_behaviour():
    settings = Settings.from_env({})
    assert settings.queue_url is None  # synchronous, as in Phase 1
    assert settings.commit_status is False
    assert settings.scan_allow_paths == ()


def test_phase_2_settings_overrides():
    settings = Settings.from_env(
        {
            "MERGEGUARD_QUEUE_URL": QUEUE,
            "MERGEGUARD_COMMIT_STATUS": "true",
            "MERGEGUARD_SCAN_ALLOW_PATHS": " tests/fixtures/** , **/*.pem ,, ",
        }
    )
    assert settings.queue_url == QUEUE
    assert settings.commit_status is True
    assert settings.scan_allow_paths == ("tests/fixtures/**", "**/*.pem")


def test_an_empty_queue_url_means_synchronous():
    assert Settings.from_env({"MERGEGUARD_QUEUE_URL": " "}).queue_url is None


@pytest.mark.parametrize(
    "value",
    [
        "http://sqs.us-east-1.amazonaws.com/123456789012/q",
        "https://evil.example.com/123456789012/q",
        "https://sqs.us-east-1.amazonaws.com.evil.example/123456789012/q",
        "https://sqs.us-east-1.amazonaws.com/123/q",
        "https://sqs.us-east-1.amazonaws.com/123456789012/q/../x",
        "mergeguard-review-requests",
    ],
)
def test_queue_url_must_be_an_sqs_url(value):
    with pytest.raises(ConfigError, match="MERGEGUARD_QUEUE_URL"):
        Settings.from_env({"MERGEGUARD_QUEUE_URL": value})


def test_commit_status_must_be_a_boolean():
    with pytest.raises(ConfigError):
        Settings.from_env({"MERGEGUARD_COMMIT_STATUS": "maybe"})


@pytest.mark.parametrize(
    "value",
    [
        "/etc/**",  # absolute
        "../outside/**",
        "a/../b",
        "**",  # would switch the gate off
        "*",
        "**/*",
        "a\tb",
        "a" * 201,
        "tests/fixtures/**,**",  # one bad entry rejects the whole list
    ],
)
def test_invalid_allow_paths_fail_closed(value):
    with pytest.raises(ConfigError, match="MERGEGUARD_SCAN_ALLOW_PATHS"):
        Settings.from_env({"MERGEGUARD_SCAN_ALLOW_PATHS": value})


def test_too_many_allow_paths_fail_closed():
    value = ",".join(f"dir{i}/**" for i in range(51))
    with pytest.raises(ConfigError, match="at most 50"):
        Settings.from_env({"MERGEGUARD_SCAN_ALLOW_PATHS": value})


def test_each_secret_can_be_read_alone():
    """The receiver may read only the webhook secret, the worker only the token
    and the Gemini key (least privilege, SECURITY.md section 5.3)."""
    ssm = FakeSsm()
    store = SecretStore("/mergeguard", lambda: ssm)
    assert store.webhook_secret() == TEST_WEBHOOK_SECRET
    assert store.github_token() == TEST_GITHUB_TOKEN
    assert [call["Names"] for call in ssm.calls] == [
        ["/mergeguard/webhook_secret"],
        ["/mergeguard/github_token"],
    ]


def test_receiver_does_not_need_the_token_to_exist():
    ssm = FakeSsm({"/mergeguard/webhook_secret": TEST_WEBHOOK_SECRET})
    store = SecretStore("/mergeguard", lambda: ssm)
    assert store.webhook_secret() == TEST_WEBHOOK_SECRET
    with pytest.raises(ConfigError):
        store.github_token()
