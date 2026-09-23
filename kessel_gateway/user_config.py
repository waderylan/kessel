"""Persistent per-user configuration for the Kessel CLI and server."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def config_directory() -> Path:
    override = os.getenv("KESSEL_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "Kessel"
    root = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "kessel"


def state_directory() -> Path:
    override = os.getenv("KESSEL_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return config_directory()
    root = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "kessel"


@dataclass(frozen=True)
class UserConfig:
    api_key: str | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    codex_command: str | None = None
    claude_command: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def path(self) -> Path:
        return config_directory() / "config.json"

    def save(self) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            path.parent.chmod(0o700)
        temporary = path.with_name(
            f".{path.name}.{secrets.token_hex(8)}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        try:
            payload = (json.dumps(asdict(self), indent=2) + "\n").encode("utf-8")
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            if os.name != "nt":
                path.chmod(0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def with_generated_key(self) -> "UserConfig":
        if self.api_key:
            return self
        return UserConfig(
            api_key="kessel_" + secrets.token_urlsafe(32),
            host=self.host,
            port=self.port,
            codex_command=self.codex_command,
            claude_command=self.claude_command,
        )

    def with_rotated_key(self) -> "UserConfig":
        return UserConfig(
            api_key="kessel_" + secrets.token_urlsafe(32),
            host=self.host,
            port=self.port,
            codex_command=self.codex_command,
            claude_command=self.claude_command,
        )

    @classmethod
    def load(cls) -> "UserConfig":
        path = config_directory() / "config.json"
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            host = str(data.get("host", DEFAULT_HOST))
            port = int(data.get("port", DEFAULT_PORT))
            api_key = data.get("api_key")
            codex_command = data.get("codex_command")
            claude_command = data.get("claude_command")
            if host not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("host must be localhost")
            if not 1 <= port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            if api_key is not None and not isinstance(api_key, str):
                raise ValueError("api_key must be a string")
            if codex_command is not None and not isinstance(codex_command, str):
                raise ValueError("codex_command must be a string")
            if claude_command is not None and not isinstance(claude_command, str):
                raise ValueError("claude_command must be a string")
            return cls(
                api_key=api_key,
                host=host,
                port=port,
                codex_command=codex_command,
                claude_command=claude_command,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid Kessel config at {path}: {exc}") from exc


def load_or_create_config() -> tuple[UserConfig, bool]:
    config = UserConfig.load()
    if config.api_key:
        return config, False
    config = config.with_generated_key()
    config.save()
    return config, True
