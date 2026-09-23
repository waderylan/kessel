"""Provider-specific installation and authentication checks."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kessel_gateway.providers.compatibility import (
    KNOWN_STABLE_VERSIONS,
    MINIMUM_VERSIONS,
    UnsupportedCliVersionError,
    capability_probes,
    known_stable_install_command,
    missing_capabilities,
    parse_version,
    require_minimum_version,
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
    )


def _resolve_executable(command: str) -> str | None:
    """Resolve provider commands to binaries safe for shell-free execution."""

    executable = shutil.which(command)
    if executable is None:
        return None
    shim = Path(executable)
    if (
        shim.stem.lower() != "codex"
        or shim.suffix.lower() not in {".cmd", ".bat"}
    ):
        return executable

    package_root = (
        shim.parent
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
    )
    arm64 = platform.machine().lower() in {"arm64", "aarch64"}
    package = "codex-win32-arm64" if arm64 else "codex-win32-x64"
    target = "aarch64-pc-windows-msvc" if arm64 else "x86_64-pc-windows-msvc"
    native = (
        package_root / package / "vendor" / target / "bin" / "codex.exe"
    )
    if native.is_file():
        return str(native.resolve())
    return executable


def _check_provider(
    name: str,
    display_name: str,
    command: str,
    auth_args: list[str],
) -> ProviderHealth:
    executable = _resolve_executable(command)
    if executable is None:
        return ProviderHealth(name, display_name, False, False)
    version = None
    try:
        version_result = _run([executable, "--version"])
        version_output = version_result.stdout + version_result.stderr
        version = parse_version(version_output)
        require_minimum_version(version, MINIMUM_VERSIONS[name])
        for index, probe in enumerate(capability_probes(name)):
            help_result = _run([executable, *probe.args])
            if help_result.returncode != 0:
                raise UnsupportedCliVersionError(
                    f"capability check failed: {' '.join(probe.args)}"
                )
            missing = missing_capabilities(
                name, help_result.stdout + help_result.stderr, index
            )
            if missing:
                raise UnsupportedCliVersionError(
                    "missing required capabilities: " + ", ".join(missing)
                )
        auth_result = _run([executable, *auth_args])
    except UnsupportedCliVersionError as exc:
        return ProviderHealth(
            name,
            display_name,
            True,
            False,
            version=version,
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
