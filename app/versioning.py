"""Strict startup checks for the provider CLIs Kessel was tested against."""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass

from app.config import Settings


class UnsupportedCliVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class CliVersion:
    command: str
    expected: str
    actual: str


def parse_version(output: str) -> str:
    match = re.search(r"\b(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)\b", output)
    if match is None:
        raise UnsupportedCliVersionError(
            f"could not parse CLI version from {output.strip()!r}"
        )
    return match.group(1)


async def _read_version(command: str, expected: str) -> CliVersion:
    executable = shutil.which(command)
    if executable is None:
        raise UnsupportedCliVersionError(f"required command not found: {command}")
    process = await asyncio.create_subprocess_exec(
        executable,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    output = (stdout + stderr).decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise UnsupportedCliVersionError(
            f"{command} --version exited with code {process.returncode}"
        )
    return CliVersion(command=command, expected=expected, actual=parse_version(output))


async def verify_cli_versions(settings: Settings) -> dict[str, CliVersion]:
    versions = await asyncio.gather(
        _read_version(settings.codex_command, settings.expected_codex_version),
        _read_version(settings.claude_command, settings.expected_claude_version),
    )
    result = {"codex": versions[0], "claude": versions[1]}
    mismatches = [
        f"{name} {version.actual} (tested: {version.expected})"
        for name, version in result.items()
        if version.actual != version.expected
    ]
    if mismatches:
        raise UnsupportedCliVersionError(
            "Refusing to start with untested CLI versions: " + ", ".join(mismatches)
        )
    return result
