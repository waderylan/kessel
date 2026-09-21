import pytest

from app import versioning
from app.config import Settings
from app.runner import ProcessNotFoundError
from app.versioning import (
    CliVersion,
    UnsupportedCliVersionError,
    parse_version,
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


@pytest.mark.asyncio
async def test_missing_provider_version_is_skipped(monkeypatch) -> None:
    async def missing(*args, **kwargs):
        raise ProcessNotFoundError("command not found")

    monkeypatch.setattr(versioning.ProcessRunner, "run", missing)

    assert await versioning._read_version("missing", "1.0.0") is None


@pytest.mark.asyncio
async def test_startup_check_rejects_untested_version(monkeypatch) -> None:
    async def fake_read_version(command: str, expected: str) -> CliVersion:
        actual = "9.9.9" if command == "codex" else expected
        return CliVersion(command=command, expected=expected, actual=actual)

    monkeypatch.setattr("app.versioning._read_version", fake_read_version)
    settings = Settings(
        api_key=None,
        cors_origins=(),
        request_timeout_seconds=10,
        max_concurrent_requests=1,
        max_output_bytes=1000,
        codex_command="codex",
        claude_command="claude",
    )

    with pytest.raises(UnsupportedCliVersionError, match="untested CLI versions"):
        await verify_cli_versions(settings)


@pytest.mark.asyncio
async def test_startup_check_accepts_one_installed_provider(monkeypatch) -> None:
    async def fake_read_version(
        command: str, expected: str
    ) -> CliVersion | None:
        if command == "codex":
            return None
        return CliVersion(command=command, expected=expected, actual=expected)

    monkeypatch.setattr("app.versioning._read_version", fake_read_version)
    settings = Settings(
        api_key=None,
        cors_origins=(),
        request_timeout_seconds=10,
        max_concurrent_requests=1,
        max_output_bytes=1000,
        codex_command="codex",
        claude_command="claude",
    )

    versions = await verify_cli_versions(settings)

    assert set(versions) == {"claude"}


@pytest.mark.asyncio
async def test_startup_check_rejects_zero_installed_providers(monkeypatch) -> None:
    async def missing(command: str, expected: str) -> None:
        return None

    monkeypatch.setattr("app.versioning._read_version", missing)
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
