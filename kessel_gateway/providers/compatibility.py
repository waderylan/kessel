"""Provider CLI compatibility requirements."""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from kessel_gateway import __version__
from kessel_gateway.user_config import state_directory


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
                "--json-schema",
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


async def probe_compatibility(
    provider: str,
    minimum: str,
    run: Callable[[list[str]], Awaitable[str]],
) -> str:
    """Run the shared version and capability probe sequence for a provider CLI.

    `run` takes CLI arguments (excluding the executable itself) and returns
    the combined stdout+stderr text, raising `UnsupportedCliVersionError` for
    a failed invocation. Used by both the synchronous `doctor`-style health
    check and the async startup version check so the two do not drift.
    Returns the parsed version string. Any `UnsupportedCliVersionError` this
    raises carries the parsed version (if one was found) on a `.version`
    attribute, so callers that want to report a stale-but-known version on
    failure still can.
    """

    version = parse_version(await run(["--version"]))
    try:
        require_minimum_version(version, minimum)
        for index, probe in enumerate(capability_probes(provider)):
            output = await run(list(probe.args))
            missing = missing_capabilities(provider, output, index)
            if missing:
                raise UnsupportedCliVersionError(
                    f"{provider} {version} is missing required capabilities: "
                    + ", ".join(missing)
                )
    except UnsupportedCliVersionError as exc:
        exc.version = version  # type: ignore[attr-defined]
        raise
    return version


def _compat_cache_path() -> Path:
    return state_directory() / "provider-compat.json"


def _compat_cache_key(executable: str, stat_result: os.stat_result) -> dict[str, object]:
    return {
        "executable": executable,
        "mtime_ns": stat_result.st_mtime_ns,
        "size": stat_result.st_size,
        "kessel_version": __version__,
    }


def load_cached_version(provider: str, executable: str) -> str | None:
    """Return a previously confirmed-compatible version, or None on any miss.

    A miss covers: no cache file, a corrupt cache file, no entry for this
    provider, or an entry whose executable/mtime/size/Kessel version no
    longer match. Auth status is never part of this cache.
    """

    try:
        raw = _compat_cache_path().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(provider)
    if not isinstance(entry, dict):
        return None
    try:
        stat_result = os.stat(executable)
    except OSError:
        return None
    key = _compat_cache_key(executable, stat_result)
    if any(entry.get(field) != value for field, value in key.items()):
        return None
    version = entry.get("version")
    return version if isinstance(version, str) else None


def store_cached_version(provider: str, executable: str, version: str) -> None:
    """Persist a confirmed-compatible version for `executable`.

    Only compatible probe results are ever passed here; auth status is
    never written to this cache. Writes atomically with private (0600)
    permissions on POSIX, matching `UserConfig.save`. The cache is only an
    optimization, so a write failure is ignored rather than failing startup.
    """

    try:
        _store_cached_version(provider, executable, version)
    except OSError:
        pass


def _store_cached_version(provider: str, executable: str, version: str) -> None:

    try:
        stat_result = os.stat(executable)
    except OSError:
        return

    path = _compat_cache_path()
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError):
        existing = {}

    entry = _compat_cache_key(executable, stat_result)
    entry["version"] = version
    existing[provider] = entry

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        payload = json.dumps(existing).encode("utf-8")
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
