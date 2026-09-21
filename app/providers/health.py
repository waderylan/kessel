"""Provider-specific installation and authentication checks."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderHealth:
    name: str
    display_name: str
    installed: bool
    authenticated: bool
    version: str | None = None
    detail: str | None = None
    executable: str | None = None

    @property
    def working(self) -> bool:
        return self.installed and self.authenticated

    @property
    def fix_command(self) -> str | None:
        if not self.installed:
            return {
                "codex": "npm install -g @openai/codex",
                "claude": "npm install -g @anthropic-ai/claude-code",
            }[self.name]
        if not self.authenticated:
            return {"codex": "codex login", "claude": "claude login"}[self.name]
        return None


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        shell=False,
    )


def _check_provider(
    name: str,
    display_name: str,
    command: str,
    auth_args: list[str],
) -> ProviderHealth:
    executable = shutil.which(command)
    if executable is None:
        return ProviderHealth(name, display_name, False, False)
    try:
        version_result = _run([command, "--version"])
        version = (version_result.stdout or version_result.stderr).strip() or None
        auth_result = _run([command, *auth_args])
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

    return [
        _check_provider("claude", "Claude Code", "claude", ["auth", "status"]),
        _check_provider("codex", "Codex", "codex", ["login", "status"]),
    ]
