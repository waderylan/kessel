"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from kessel_gateway.providers.compatibility import MINIMUM_VERSIONS
from kessel_gateway.user_config import UserConfig


def _positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the HTTP server and provider processes."""

    api_key: str | None
    cors_origins: tuple[str, ...]
    request_timeout_seconds: int
    max_concurrent_requests: int
    max_output_bytes: int
    codex_command: str
    claude_command: str
    enforce_cli_versions: bool = True
    minimum_codex_version: str = MINIMUM_VERSIONS["codex"]
    minimum_claude_version: str = MINIMUM_VERSIONS["claude"]
    codex_max_concurrent_requests: int | None = None
    claude_max_concurrent_requests: int | None = None
    provider_slot_wait_seconds: int = 5
    shutdown_grace_seconds: int = 5
    max_request_bytes: int = 1_048_576
    listen_host: str = "127.0.0.1"
    listen_port: int = 8000
    reload_api_key_from_config: bool = False
    allow_unauthenticated: bool = False

    def provider_limit(self, provider: str) -> int:
        configured = {
            "codex": self.codex_max_concurrent_requests,
            "claude": self.claude_max_concurrent_requests,
        }.get(provider)
        return configured or self.max_concurrent_requests

    @classmethod
    def from_environment(cls) -> "Settings":
        user_config = UserConfig.load()
        environment_key = os.getenv("KESSEL_API_KEY") or None
        shared_limit = _positive_int("KESSEL_MAX_CONCURRENT_REQUESTS", 2)
        origins = tuple(
            origin.strip()
            for origin in os.getenv(
                "KESSEL_CORS_ORIGINS",
                (
                    f"http://127.0.0.1:{user_config.port},"
                    f"http://localhost:{user_config.port},"
                    f"http://[::1]:{user_config.port}"
                ),
            ).split(",")
            if origin.strip()
        )
        return cls(
            api_key=environment_key or user_config.api_key,
            cors_origins=origins,
            request_timeout_seconds=_positive_int(
                "KESSEL_REQUEST_TIMEOUT_SECONDS", 300
            ),
            max_concurrent_requests=shared_limit,
            max_output_bytes=_positive_int("KESSEL_MAX_OUTPUT_BYTES", 1_048_576),
            codex_command=(
                os.getenv("KESSEL_CODEX_COMMAND")
                or user_config.codex_command
                or "codex"
            ),
            claude_command=(
                os.getenv("KESSEL_CLAUDE_COMMAND")
                or user_config.claude_command
                or "claude"
            ),
            enforce_cli_versions=_boolean("KESSEL_ENFORCE_CLI_VERSIONS", True),
            codex_max_concurrent_requests=_positive_int(
                "KESSEL_CODEX_MAX_CONCURRENT_REQUESTS", shared_limit
            ),
            claude_max_concurrent_requests=_positive_int(
                "KESSEL_CLAUDE_MAX_CONCURRENT_REQUESTS", shared_limit
            ),
            provider_slot_wait_seconds=_positive_int(
                "KESSEL_PROVIDER_SLOT_WAIT_SECONDS", 5
            ),
            shutdown_grace_seconds=_positive_int(
                "KESSEL_SHUTDOWN_GRACE_SECONDS", 5
            ),
            max_request_bytes=_positive_int(
                "KESSEL_MAX_REQUEST_BYTES", 1_048_576
            ),
            listen_host=user_config.host,
            listen_port=user_config.port,
            reload_api_key_from_config=environment_key is None,
        )


settings = Settings.from_environment()
