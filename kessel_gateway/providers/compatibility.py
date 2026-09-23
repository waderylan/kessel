"""Provider CLI compatibility requirements."""

from __future__ import annotations

import re
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version


class UnsupportedCliVersionError(RuntimeError):
    pass


MINIMUM_VERSIONS = {
    "codex": "0.155.1",
    "claude": "2.1.278",
}

KNOWN_STABLE_VERSIONS = {
    "codex": "0.155.1",
    "claude": "2.1.280",
}

PROVIDER_PACKAGES = {
    "codex": "@openai/codex",
    "claude": "@anthropic-ai/claude-code",
}

PROVIDER_DISPLAY_NAMES = {
    "codex": "Codex",
    "claude": "Claude Code",
}


@dataclass(frozen=True)
class CapabilityProbe:
    args: tuple[str, ...]
    required_markers: tuple[str, ...]


CAPABILITY_PROBES = {
    "codex": (
        CapabilityProbe(
            args=("exec", "--help"),
            required_markers=(
                "--json",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--config",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "--disable",
                "--enable",
                "--output-schema",
                "--model",
            ),
        ),
        CapabilityProbe(
            args=("app-server", "--help"),
            required_markers=("--stdio", "--config", "--disable"),
        ),
    ),
    "claude": (
        CapabilityProbe(
            args=("--help",),
            required_markers=(
                "--print",
                "--output-format",
                "--no-session-persistence",
                "--permission-prompts",
                "--safe-mode",
                "--restricted",
                "--tools",
                "--system-prompt",
                "--verbose",
                "--include-partial-messages",
            ),
        ),
        CapabilityProbe(
            args=("auth", "status", "--help"),
            required_markers=("--json",),
        ),
    ),
}


def capability_probes(provider: str) -> tuple[CapabilityProbe, ...]:
    try:
        return CAPABILITY_PROBES[provider]
    except KeyError as exc:
        raise ValueError(f"unknown provider: {provider}") from exc


def known_stable_install_command(provider: str) -> str:
    try:
        package = PROVIDER_PACKAGES[provider]
        version = KNOWN_STABLE_VERSIONS[provider]
    except KeyError as exc:
        raise ValueError(f"unknown provider: {provider}") from exc
    return f"npm install -g {package}@{version}"


def known_stable_guidance(provider: str) -> str:
    try:
        display_name = PROVIDER_DISPLAY_NAMES[provider]
        version = KNOWN_STABLE_VERSIONS[provider]
    except KeyError as exc:
        raise ValueError(f"unknown provider: {provider}") from exc
    return (
        f"Known stable {display_name} version: {version}. "
        f"Install it with: {known_stable_install_command(provider)}"
    )


def parse_version(output: str) -> str:
    match = re.search(r"\b(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)\b", output)
    if match is None:
        raise UnsupportedCliVersionError("could not parse provider CLI version")
    return match.group(1)


def require_minimum_version(actual: str, minimum: str) -> None:
    try:
        actual_version = Version(actual)
        minimum_version = Version(minimum)
    except InvalidVersion as exc:
        raise UnsupportedCliVersionError("provider CLI version is invalid") from exc
    if actual_version < minimum_version:
        raise UnsupportedCliVersionError(
            f"version {actual} is below the required minimum {minimum}"
        )


def missing_capabilities(
    provider: str, output: str, probe_index: int
) -> tuple[str, ...]:
    probe = capability_probes(provider)[probe_index]
    return tuple(
        marker for marker in probe.required_markers if marker not in output
    )
