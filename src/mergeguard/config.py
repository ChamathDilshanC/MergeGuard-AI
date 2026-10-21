"""Runtime configuration: environment settings and cached SSM secrets (FR-011)."""

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_PARAM_PREFIX = "/mergeguard"
DEFAULT_MAX_FILES = 300
GITHUB_MAX_FILES = 3000  # GitHub's limit for the pull request files API
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_MAX_DIFF_CHARS = 12_000  # from the brief
MAX_DIFF_CHARS_LIMIT = 200_000
SECRET_TTL_SECONDS = 900.0  # 15 minutes, so rotations apply without a redeploy
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
GITHUB_TOKEN_NAME = "github_token"  # noqa: S105 - a parameter name, not a secret
WEBHOOK_SECRET_NAME = "webhook_secret"  # noqa: S105 - a parameter name, not a secret
# Needed for every delivery; loaded first and fail closed.
CORE_SECRET_NAMES = (GITHUB_TOKEN_NAME, WEBHOOK_SECRET_NAME)
# Loaded only when an AI review is actually requested, so a missing Gemini key
# degrades to the AI-unavailable comment instead of blocking the secret gate.
GEMINI_KEY_NAME = "gemini_api_key"
# Phase 3 (FR-020): the GitHub App's private key, read by the worker only when
# MERGEGUARD_GITHUB_APP_ID is set. In App mode the personal access token is never
# read.
GITHUB_APP_KEY_NAME = "github_app_private_key"
# Terraform creates each parameter with a public placeholder value (ADR-0007).
# A value starting with this prefix counts as "not set": core secrets fail
# closed, and a placeholder Gemini key means no diff is ever sent to Gemini.
PLACEHOLDER_PREFIX = "mergeguard-placeholder"

INLINE_COMMENT_MODES = ("off", "secrets", "all")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")
DEFAULT_REPO_CONFIG_BLOCK_LEVELS = ("MEDIUM", "HIGH")
MAX_ALLOW_PATHS = 50
MAX_ALLOW_PATH_LENGTH = 200

_TRUE = frozenset({"1", "true", "yes"})
_FALSE = frozenset({"0", "false", "no"})
# The model ID becomes part of the Gemini URL path, so it is strictly validated.
_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
# The receiver only ever talks to this SQS queue (FR-016).
_QUEUE_URL_RE = re.compile(
    r"https://sqs\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/\d{12}/[A-Za-z0-9_-]{1,80}"
)
_ALLOW_PATH_RE = re.compile(r"[A-Za-z0-9_./*?@+=~ -]+")
# A GitHub App ID (digits) or client ID (for example Iv1.abc); it is not a secret.
_APP_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")


class ConfigError(Exception):
    """Configuration or secrets are missing or invalid; processing must stop."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Non-secret settings from ``MERGEGUARD_*`` environment variables."""

    param_prefix: str
    max_files: int
    log_level: str
    dry_run: bool
    gemini_model: str = DEFAULT_GEMINI_MODEL
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS
    # Phase 2. A queue URL turns the handler into the receiver (FR-016); unset
    # keeps the synchronous Phase 1 behaviour used by tests and local runs.
    queue_url: str | None = None
    # FR-017. Off unless enabled: the token also needs "Commit statuses: write".
    commit_status: bool = False
    # FR-019. Operator-defined path globs whose findings are suppressed.
    scan_allow_paths: tuple[str, ...] = ()
    # Phase 3. An App ID switches the worker from the personal access token to
    # GitHub App installation tokens (FR-020).
    github_app_id: str | None = None
    # FR-021: "off", "secrets" (inline comments for secret findings only) or "all"
    # (also for AI findings, which needs the longer review-v2 prompt).
    inline_comments: str = "off"
    # FR-022: read .github/mergeguard.json from the pull request's base commit.
    repo_config: bool = False
    # The blocking levels a repository file may choose (operator-defined bounds).
    repo_config_block_levels: tuple[str, ...] = DEFAULT_REPO_CONFIG_BLOCK_LEVELS

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        prefix = environ.get("MERGEGUARD_PARAM_PREFIX", DEFAULT_PARAM_PREFIX)
        if not prefix.startswith("/") or prefix.endswith("/") or len(prefix) < 2:
            raise ConfigError("MERGEGUARD_PARAM_PREFIX must look like /name")
        level = environ.get("MERGEGUARD_LOG_LEVEL", "INFO").upper()
        if level not in LOG_LEVELS:
            raise ConfigError(f"MERGEGUARD_LOG_LEVEL must be one of {LOG_LEVELS}")
        model = environ.get("MERGEGUARD_GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        if not _MODEL_RE.fullmatch(model):
            raise ConfigError("MERGEGUARD_GEMINI_MODEL is not a valid model ID")
        return cls(
            param_prefix=prefix,
            max_files=_int_setting(
                environ, "MERGEGUARD_MAX_FILES", DEFAULT_MAX_FILES, GITHUB_MAX_FILES
            ),
            log_level=level,
            dry_run=_bool_setting(environ, "MERGEGUARD_DRY_RUN"),
            gemini_model=model,
            max_diff_chars=_int_setting(
                environ,
                "MERGEGUARD_MAX_DIFF_CHARS",
                DEFAULT_MAX_DIFF_CHARS,
                MAX_DIFF_CHARS_LIMIT,
            ),
            queue_url=_queue_url_setting(environ),
            commit_status=_bool_setting(environ, "MERGEGUARD_COMMIT_STATUS"),
            scan_allow_paths=_allow_paths_setting(environ),
            github_app_id=_app_id_setting(environ),
            inline_comments=_inline_comments_setting(environ),
            repo_config=_bool_setting(environ, "MERGEGUARD_REPO_CONFIG"),
            repo_config_block_levels=_block_levels_setting(environ),
        )


@dataclass(frozen=True, slots=True)
class Secrets:
    """Secret values. Excluded from repr so they never reach logs or tracebacks."""

    github_token: str = field(repr=False)
    webhook_secret: str = field(repr=False)


class SsmClient(Protocol):
    def get_parameters(
        self, *, Names: list[str], WithDecryption: bool
    ) -> Mapping[str, Any]: ...


def _default_ssm_client() -> SsmClient:
    import boto3  # bundled with the Lambda runtime (ADR-0002)

    return boto3.client("ssm")


class SecretStore:
    """Loads secrets from SSM and caches them per execution environment for
    ``ttl_seconds``. The core secrets and the Gemini key are separate
    ``GetParameters`` calls; any problem raises ConfigError."""

    def __init__(
        self,
        prefix: str,
        client_factory: Callable[[], SsmClient] = _default_ssm_client,
        *,
        ttl_seconds: float = SECRET_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.prefix = prefix
        self._client_factory = client_factory
        self._client: SsmClient | None = None
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._cache: dict[tuple[str, ...], tuple[float, dict[str, str]]] = {}

    def get(self) -> Secrets:
        """The secrets every delivery needs (FR-011). Fails closed."""
        return Secrets(**self._values(CORE_SECRET_NAMES))

    def webhook_secret(self) -> str:
        """Only the webhook secret: all the receiver is allowed to read."""
        return self._values((WEBHOOK_SECRET_NAME,))[WEBHOOK_SECRET_NAME]

    def github_token(self) -> str:
        """Only the GitHub token: what the worker needs besides the Gemini key."""
        return self._values((GITHUB_TOKEN_NAME,))[GITHUB_TOKEN_NAME]

    def github_app_private_key(self) -> str:
        """The GitHub App's private key (PEM), read only in App mode."""
        return self._values((GITHUB_APP_KEY_NAME,))[GITHUB_APP_KEY_NAME]

    def gemini_api_key(self) -> str:
        """The Gemini key, read only when an AI review is requested (M3)."""
        return self._values((GEMINI_KEY_NAME,))[GEMINI_KEY_NAME]

    def _values(self, names: Sequence[str]) -> dict[str, str]:
        key = tuple(names)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is None or now - cached[0] >= self._ttl_seconds:
            cached = (now, self._load(key))
            self._cache[key] = cached
        return cached[1]

    def _load(self, names: tuple[str, ...]) -> dict[str, str]:
        full_names = [f"{self.prefix}/{name}" for name in names]
        try:
            if self._client is None:
                self._client = self._client_factory()
            response = self._client.get_parameters(
                Names=full_names, WithDecryption=True
            )
        except Exception as exc:
            raise ConfigError("could not read parameters from SSM") from exc
        invalid = response.get("InvalidParameters") or []
        if invalid:
            raise ConfigError("missing SSM parameters: " + ", ".join(sorted(invalid)))
        values = {
            item.get("Name"): item.get("Value")
            for item in response.get("Parameters") or []
        }
        resolved: dict[str, str] = {}
        for name, full_name in zip(names, full_names, strict=True):
            value = values.get(full_name)
            if not isinstance(value, str) or not value:
                raise ConfigError(f"SSM parameter {full_name} is missing or empty")
            if value.startswith(PLACEHOLDER_PREFIX):
                raise ConfigError(
                    f"SSM parameter {full_name} still holds the placeholder"
                )
            resolved[name] = value
        return resolved


def _int_setting(
    environ: Mapping[str, str], name: str, default: int, maximum: int
) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise ConfigError(f"{name} must be between 1 and {maximum}")
    return value


def _queue_url_setting(environ: Mapping[str, str]) -> str | None:
    raw = environ.get("MERGEGUARD_QUEUE_URL", "").strip()
    if not raw:
        return None
    if not _QUEUE_URL_RE.fullmatch(raw):
        raise ConfigError("MERGEGUARD_QUEUE_URL must be an SQS queue URL")
    return raw


def _allow_paths_setting(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Comma-separated path globs. Invalid entries fail closed, so a typo can
    never silently widen what the scanner ignores."""
    patterns = tuple(
        item.strip()
        for item in environ.get("MERGEGUARD_SCAN_ALLOW_PATHS", "").split(",")
        if item.strip()
    )
    if len(patterns) > MAX_ALLOW_PATHS:
        raise ConfigError(
            f"MERGEGUARD_SCAN_ALLOW_PATHS allows at most {MAX_ALLOW_PATHS} patterns"
        )
    for pattern in patterns:
        if not is_valid_path_pattern(pattern):
            raise ConfigError(
                "MERGEGUARD_SCAN_ALLOW_PATHS has an invalid pattern; use relative "
                "path globs such as tests/fixtures/**"
            )
    return patterns


def is_valid_path_pattern(pattern: str) -> bool:
    """A relative path glob that names something: no absolute path, no ``..``,
    and not only wildcards (which would match everything). Shared by the operator
    allow-list and the repository configuration."""
    return bool(
        len(pattern) <= MAX_ALLOW_PATH_LENGTH
        and _ALLOW_PATH_RE.fullmatch(pattern) is not None
        and not pattern.startswith("/")
        and ".." not in pattern
        and pattern.strip("*?/")
    )


def _app_id_setting(environ: Mapping[str, str]) -> str | None:
    raw = environ.get("MERGEGUARD_GITHUB_APP_ID", "").strip()
    if not raw:
        return None
    if not _APP_ID_RE.fullmatch(raw):
        raise ConfigError("MERGEGUARD_GITHUB_APP_ID must be a GitHub App ID")
    return raw


def _inline_comments_setting(environ: Mapping[str, str]) -> str:
    mode = environ.get("MERGEGUARD_INLINE_COMMENTS", "off").strip().lower()
    if mode not in INLINE_COMMENT_MODES:
        raise ConfigError(
            f"MERGEGUARD_INLINE_COMMENTS must be one of {INLINE_COMMENT_MODES}"
        )
    return mode


def _block_levels_setting(environ: Mapping[str, str]) -> tuple[str, ...]:
    raw = environ.get("MERGEGUARD_REPO_CONFIG_BLOCK_LEVELS")
    if raw is None or not raw.strip():
        return DEFAULT_REPO_CONFIG_BLOCK_LEVELS
    levels = tuple(item.strip().upper() for item in raw.split(",") if item.strip())
    if not levels or any(level not in RISK_LEVELS for level in levels):
        raise ConfigError(
            f"MERGEGUARD_REPO_CONFIG_BLOCK_LEVELS must list levels from {RISK_LEVELS}"
        )
    return tuple(dict.fromkeys(levels))  # no duplicates, order kept


def _bool_setting(environ: Mapping[str, str], name: str) -> bool:
    raw = environ.get(name, "false").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")
