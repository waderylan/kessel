import pytest

from app.config import Settings
from app.versioning import (
    CliVersion,
    UnsupportedCliVersionError,
    parse_version,
    verify_cli_versions,
)


def test_parse_codex_and_claude_versions() -> None:
    assert parse_version("codex-cli 0.155.1") == "0.155.1"
    assert parse_version("2.1.278 (Claude Code)") == "2.1.278"


def test_unparseable_version_is_rejected() -> None:
    with pytest.raises(UnsupportedCliVersionError):
        parse_version("development build")


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
