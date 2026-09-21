"""Strict startup checks for the provider CLIs Kessel was tested against."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.runner import ProcessError, ProcessRunner


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
    runner = ProcessRunner(timeout_seconds=10, max_output_bytes=65_536)
    try:
        result = await runner.run([command, "--version"], "", Path("."))
    except ProcessError as exc:
        raise UnsupportedCliVersionError(str(exc)) from exc
    output = result.stdout + result.stderr
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
