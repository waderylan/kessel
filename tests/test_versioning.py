import pytest

from app import versioning
from app.config import Settings
from app.providers.compatibility import capability_probes
from app.runner import ProcessNotFoundError, ProcessResult
from app.versioning import (
    CliVersion,
    UnsupportedCliVersionError,
    parse_version,
    require_minimum_version,
    verify_cli_versions,
)


def test_version_enforcement_defaults_on(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("KESSEL_ENFORCE_CLI_VERSIONS", raising=False)
    assert Settings.from_environment().enforce_cli_versions is True


def test_parse_codex_and_claude_versions() -> None:
    assert parse_version("codex-cli 0.155.1") == "0.155.1"
    assert parse_version("2.1.278 (Claude Code)") == "2.1.278"


def test_unparseable_version_is_rejected() -> None:
    with pytest.raises(UnsupportedCliVersionError):
        parse_version("development build")


def test_minimum_version_accepts_equal_and_newer_releases() -> None:
    require_minimum_version("0.155.1", "0.155.1")
    require_minimum_version("0.156.0", "0.155.1")
    require_minimum_version("1.0.0", "0.155.1")


def test_minimum_version_rejects_older_and_prerelease_versions() -> None:
    with pytest.raises(UnsupportedCliVersionError, match="below"):
        require_minimum_version("0.155.0", "0.155.1")
    with pytest.raises(UnsupportedCliVersionError, match="below"):
        require_minimum_version("0.155.1rc1", "0.155.1")


@pytest.mark.asyncio
async def test_missing_provider_version_is_skipped(monkeypatch) -> None:
    async def missing(*args, **kwargs) -> ProcessResult:
        raise ProcessNotFoundError("command not found")

    monkeypatch.setattr(versioning.ProcessRunner, "run", missing)

    assert await versioning._inspect_cli("codex", "missing", "1.0.0") is None


@pytest.mark.asyncio
async def test_newer_version_requires_declared_capabilities(monkeypatch) -> None:
    calls: list[list[str]] = []

    async def fake_run(self, command, stdin_text, cwd, env_overrides=None):
        calls.append(command)
        if command[-1] == "--version":
            return ProcessResult(stdout="codex-cli 0.156.0", stderr="")
        probe = next(
            probe
            for probe in capability_probes("codex")
            if list(probe.args) == command[1:]
        )
        return ProcessResult(stdout=" ".join(probe.required_markers), stderr="")

    monkeypatch.setattr(versioning.ProcessRunner, "run", fake_run)

    result = await versioning._inspect_cli("codex", "codex", "0.155.1")

    assert result == CliVersion(
        command="codex", minimum="0.155.1", actual="0.156.0"
    )
    assert calls == [
        ["codex", "--version"],
        ["codex", "exec", "--help"],
        ["codex", "app-server", "--help"],
    ]


@pytest.mark.asyncio
async def test_newer_version_missing_capability_is_rejected(monkeypatch) -> None:
    async def fake_run(self, command, stdin_text, cwd, env_overrides=None):
        if command[-1] == "--version":
            return ProcessResult(stdout="codex-cli 0.156.0", stderr="")
        return ProcessResult(stdout="--json", stderr="")

    monkeypatch.setattr(versioning.ProcessRunner, "run", fake_run)

    with pytest.raises(UnsupportedCliVersionError, match="missing required"):
        await versioning._inspect_cli("codex", "codex", "0.155.1")


@pytest.mark.asyncio
async def test_startup_check_rejects_older_versions(monkeypatch) -> None:
    async def fake_inspect(
        provider: str, command: str, minimum: str
    ) -> CliVersion:
        raise UnsupportedCliVersionError(
            f"version 0.1.0 is below the required minimum {minimum}"
        )

    monkeypatch.setattr("app.versioning._inspect_cli", fake_inspect)
    settings = Settings(
        api_key=None,
        cors_origins=(),
        request_timeout_seconds=10,
        max_concurrent_requests=1,
        max_output_bytes=1000,
        codex_command="codex",
        claude_command="claude",
    )

    with pytest.raises(UnsupportedCliVersionError, match="No installed provider"):
        await verify_cli_versions(settings)


@pytest.mark.asyncio
async def test_startup_check_accepts_one_installed_provider(monkeypatch) -> None:
    async def fake_inspect(
        provider: str, command: str, minimum: str
    ) -> CliVersion | None:
        if command == "codex":
            return None
        return CliVersion(command=command, minimum=minimum, actual="9.9.9")

    monkeypatch.setattr("app.versioning._inspect_cli", fake_inspect)
    settings = Settings(
        api_key=None,
        cors_origins=(),
        request_timeout_seconds=10,
        max_concurrent_requests=1,
        max_output_bytes=1000,
        codex_command="codex",
        claude_command="claude",
    )

    report = await verify_cli_versions(settings)

    assert set(report.compatible) == {"claude"}
    assert report.incompatible == {}


@pytest.mark.asyncio
async def test_startup_check_isolates_an_incompatible_provider(monkeypatch) -> None:
    async def fake_inspect(
        provider: str, command: str, minimum: str
    ) -> CliVersion:
        if provider == "codex":
            raise UnsupportedCliVersionError("missing required capabilities: --json")
        return CliVersion(command=command, minimum=minimum, actual="9.9.9")

    monkeypatch.setattr("app.versioning._inspect_cli", fake_inspect)
    settings = Settings(
        api_key=None,
        cors_origins=(),
        request_timeout_seconds=10,
        max_concurrent_requests=1,
        max_output_bytes=1000,
        codex_command="codex",
        claude_command="claude",
    )

    report = await verify_cli_versions(settings)

    assert set(report.compatible) == {"claude"}
    assert report.incompatible == {
        "codex": "missing required capabilities: --json"
    }


@pytest.mark.asyncio
async def test_startup_check_rejects_zero_installed_providers(monkeypatch) -> None:
    async def missing(provider: str, command: str, minimum: str) -> None:
        return None

    monkeypatch.setattr("app.versioning._inspect_cli", missing)
    settings = Settings(
        api_key=None,
        cors_origins=(),
        request_timeout_seconds=10,
        max_concurrent_requests=1,
        max_output_bytes=1000,
        codex_command="codex",
        claude_command="claude",
    )

    with pytest.raises(UnsupportedCliVersionError, match="at least one"):
        await verify_cli_versions(settings)
