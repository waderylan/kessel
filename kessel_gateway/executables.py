"""Resolve provider commands to binaries safe for shell-free execution."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path


def resolve_executable(command: str) -> str | None:
    """Resolve a provider command to the binary Kessel should exec directly.

    `shutil.which` can resolve an npm-installed Codex CLI to a `.cmd`/`.bat`
    shim on Windows. Shims only work through `cmd.exe`, but Kessel always
    executes with `shell=False`, so prefer the native `codex.exe` binary that
    ships alongside the npm package when one is available. Every other
    command (including Claude) is returned exactly as `shutil.which` finds
    it.
    """

    executable = shutil.which(command)
    if executable is None:
        return None
    shim = Path(executable)
    if shim.stem.lower() != "codex" or shim.suffix.lower() not in {".cmd", ".bat"}:
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
    native = package_root / package / "vendor" / target / "bin" / "codex.exe"
    if native.is_file():
        return str(native.resolve())
    return executable
