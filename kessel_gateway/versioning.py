"""Minimum-version and capability checks for provider CLIs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from kessel_gateway.config import Settings
from kessel_gateway.executables import resolve_executable
from kessel_gateway.providers.compatibility import (
    UnsupportedCliVersionError,
    known_stable_guidance,
    load_cached_version,
    parse_version,
    probe_compatibility,
    require_minimum_version,
    store_cached_version,
)
from kessel_gateway.runner import ProcessError, ProcessRunner


@dataclass(frozen=True)
class CliVersion:
    command: str
    minimum: str
    actual: str


@dataclass(frozen=True)
class CliCompatibilityReport:
    compatible: dict[str, CliVersion]
    incompatible: dict[str, str]


async def _inspect_cli(
    provider: str,
    command: str,
    minimum: str,
) -> CliVersion | None:
    executable = await asyncio.to_thread(resolve_executable, command)
    if executable is None:
        return None

    cached_version = await asyncio.to_thread(
        load_cached_version, provider, executable
    )
    if cached_version is not None:
        return CliVersion(command=command, minimum=minimum, actual=cached_version)

    runner = ProcessRunner(timeout_seconds=10, max_output_bytes=65_536)

    async def run(args: list[str]) -> str:
        try:
            result = await runner.run([executable, *args], "", Path("."))
        except ProcessError as exc:
            raise UnsupportedCliVersionError(
                f"{provider} check failed: {' '.join(args)}"
            ) from exc
        return result.stdout + result.stderr

    actual = await probe_compatibility(provider, minimum, run)
    await asyncio.to_thread(store_cached_version, provider, executable, actual)
    return CliVersion(command=command, minimum=minimum, actual=actual)


async def verify_cli_versions(settings: Settings) -> CliCompatibilityReport:
    providers = (
        ("codex", settings.codex_command, settings.minimum_codex_version),
        ("claude", settings.claude_command, settings.minimum_claude_version),
    )
    results = await asyncio.gather(
        *(
            _inspect_cli(provider, command, minimum)
            for provider, command, minimum in providers
        ),
        return_exceptions=True,
    )

    compatible: dict[str, CliVersion] = {}
    incompatible: dict[str, str] = {}
    installed_count = 0
    for (provider, _, _), result in zip(providers, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if result is None:
            continue
        installed_count += 1
        if isinstance(result, UnsupportedCliVersionError):
            incompatible[provider] = str(result)
        elif isinstance(result, BaseException):
            raise result
        else:
            compatible[provider] = result

    if compatible:
        return CliCompatibilityReport(
            compatible=compatible,
            incompatible=incompatible,
        )
    if installed_count == 0:
        raise UnsupportedCliVersionError(
            "Kessel needs at least one installed provider CLI: Codex or "
            "Claude Code"
        )
    details = "; ".join(
        f"{provider}: {detail}. {known_stable_guidance(provider)}"
        for provider, detail in incompatible.items()
    )
    raise UnsupportedCliVersionError(
        "No installed provider CLI is compatible. " + details
    )
