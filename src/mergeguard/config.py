"""Runtime configuration: environment settings and cached SSM secrets (FR-011)."""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_PARAM_PREFIX = "/mergeguard"
DEFAULT_MAX_FILES = 300
GITHUB_MAX_FILES = 3000  # GitHub's limit for the pull request files API
SECRET_TTL_SECONDS = 900.0  # 15 minutes, so rotations apply without a redeploy
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# gemini_api_key joins this list with the AI review (M3).
SECRET_NAMES = ("github_token", "webhook_secret")

_TRUE = frozenset({"1", "true", "yes"})
_FALSE = frozenset({"0", "false", "no"})


class ConfigError(Exception):
    """Configuration or secrets are missing or invalid; processing must stop."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Non-secret settings from ``MERGEGUARD_*`` environment variables."""

    param_prefix: str
    max_files: int
    log_level: str
    dry_run: bool

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        prefix = environ.get("MERGEGUARD_PARAM_PREFIX", DEFAULT_PARAM_PREFIX)
        if not prefix.startswith("/") or prefix.endswith("/") or len(prefix) < 2:
            raise ConfigError("MERGEGUARD_PARAM_PREFIX must look like /name")
        level = environ.get("MERGEGUARD_LOG_LEVEL", "INFO").upper()
        if level not in LOG_LEVELS:
            raise ConfigError(f"MERGEGUARD_LOG_LEVEL must be one of {LOG_LEVELS}")
        return cls(
            param_prefix=prefix,
            max_files=_int_setting(environ, "MERGEGUARD_MAX_FILES", DEFAULT_MAX_FILES),
            log_level=level,
            dry_run=_bool_setting(environ, "MERGEGUARD_DRY_RUN"),
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
    """Loads the secrets with one ``GetParameters`` call and caches them per
    execution environment for ``ttl_seconds``. Any problem fails closed."""

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
        self._cached: Secrets | None = None
        self._loaded_at = 0.0

    def get(self) -> Secrets:
        now = self._clock()
        if self._cached is None or now - self._loaded_at >= self._ttl_seconds:
            self._cached = self._load()
            self._loaded_at = now
        return self._cached

    def _load(self) -> Secrets:
        names = [f"{self.prefix}/{name}" for name in SECRET_NAMES]
        try:
            if self._client is None:
                self._client = self._client_factory()
            response = self._client.get_parameters(Names=names, WithDecryption=True)
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
        for name, full_name in zip(SECRET_NAMES, names, strict=True):
            value = values.get(full_name)
            if not isinstance(value, str) or not value:
                raise ConfigError(f"SSM parameter {full_name} is missing or empty")
            resolved[name] = value
        return Secrets(**resolved)


def _int_setting(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not 1 <= value <= GITHUB_MAX_FILES:
        raise ConfigError(f"{name} must be between 1 and {GITHUB_MAX_FILES}")
    return value


def _bool_setting(environ: Mapping[str, str], name: str) -> bool:
    raw = environ.get(name, "false").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")
