"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


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
    expected_codex_version: str = "0.155.1"
    expected_claude_version: str = "2.1.278"

    @classmethod
    def from_environment(cls) -> "Settings":
        origins = tuple(
            origin.strip()
            for origin in os.getenv(
                "KESSEL_CORS_ORIGINS",
                "http://127.0.0.1:8000,http://localhost:8000",
            ).split(",")
            if origin.strip()
        )
        return cls(
            api_key=os.getenv("KESSEL_API_KEY") or None,
            cors_origins=origins,
            request_timeout_seconds=_positive_int(
                "KESSEL_REQUEST_TIMEOUT_SECONDS", 300
            ),
            max_concurrent_requests=_positive_int(
                "KESSEL_MAX_CONCURRENT_REQUESTS", 2
            ),
            max_output_bytes=_positive_int("KESSEL_MAX_OUTPUT_BYTES", 1_048_576),
            codex_command=os.getenv("KESSEL_CODEX_COMMAND", "codex"),
            claude_command=os.getenv("KESSEL_CLAUDE_COMMAND", "claude"),
            enforce_cli_versions=_boolean("KESSEL_ENFORCE_CLI_VERSIONS", True),
        )


settings = Settings.from_environment()
