"""Provider-specific installation and authentication checks."""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass

from kessel_gateway.executables import resolve_executable as _resolve_executable
from kessel_gateway.process_security import child_environment
from kessel_gateway.runner import hidden_process_options
from kessel_gateway.providers.compatibility import (
    KNOWN_STABLE_VERSIONS,
    MINIMUM_VERSIONS,
    UnsupportedCliVersionError,
    known_stable_install_command,
    load_cached_version,
    probe_compatibility,
    store_cached_version,
)
from kessel_gateway.user_config import UserConfig


@dataclass(frozen=True)
class ProviderHealth:
    name: str
    display_name: str
    installed: bool
    authenticated: bool
    version: str | None = None
    detail: str | None = None
    executable: str | None = None
    compatible: bool = True

    @property
    def working(self) -> bool:
        return self.installed and self.authenticated and self.compatible

    @property
    def fix_command(self) -> str | None:
        if not self.installed:
            return {
                "codex": "npm install -g @openai/codex",
                "claude": "npm install -g @anthropic-ai/claude-code",
            }[self.name]
        if not self.compatible:
            return known_stable_install_command(self.name)
        if not self.authenticated:
            return {"codex": "codex login", "claude": "claude login"}[self.name]
        return None

    @property
    def known_stable_version(self) -> str:
        return KNOWN_STABLE_VERSIONS[self.name]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        shell=False,
        env=child_environment(),
        **hidden_process_options(),
    )


async def _probe(name: str, executable: str) -> str:
    """Drive the shared async compatibility probe from sync `_check_provider`."""

    async def run(args: list[str]) -> str:
        result = await asyncio.to_thread(_run, [executable, *args])
        if result.returncode != 0:
            label = "version check" if args == ["--version"] else "capability check"
            raise UnsupportedCliVersionError(
                f"{label} failed: {' '.join(args)}"
            )
        return result.stdout + result.stderr

    return await probe_compatibility(name, MINIMUM_VERSIONS[name], run)


def _check_provider(
    name: str,
    display_name: str,
    command: str,
    auth_args: list[str],
) -> ProviderHealth:
    executable = _resolve_executable(command)
    if executable is None:
        return ProviderHealth(name, display_name, False, False)

    version = load_cached_version(name, executable)
    if version is None:
        try:
            version = asyncio.run(_probe(name, executable))
        except UnsupportedCliVersionError as exc:
            return ProviderHealth(
                name,
                display_name,
                True,
                False,
                version=getattr(exc, "version", None),
                detail=str(exc),
                executable=executable,
                compatible=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProviderHealth(
                name,
                display_name,
                True,
                False,
                detail=str(exc),
                executable=executable,
            )
        store_cached_version(name, executable, version)

    try:
        auth_result = _run([executable, *auth_args])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProviderHealth(
            name,
            display_name,
            True,
            False,
            detail=str(exc),
            executable=executable,
        )
    detail = (auth_result.stderr or auth_result.stdout).strip() or None
    return ProviderHealth(
        name,
        display_name,
        True,
        auth_result.returncode == 0,
        version=version,
        detail=detail,
        executable=executable,
    )


def check_providers() -> list[ProviderHealth]:
    """Return deterministic checks for both supported local CLIs."""

    config = UserConfig.load()
    claude_command = (
        os.getenv("KESSEL_CLAUDE_COMMAND")
        or config.claude_command
        or "claude"
    )
    codex_command = (
        os.getenv("KESSEL_CODEX_COMMAND") or config.codex_command or "codex"
    )
    return [
        _check_provider(
            "claude", "Claude Code", claude_command, ["auth", "status"]
        ),
        _check_provider(
            "codex", "Codex", codex_command, ["login", "status"]
        ),
    ]
