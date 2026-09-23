from pathlib import Path
from subprocess import CompletedProcess

from kessel_gateway import executables
from kessel_gateway.providers import health
from kessel_gateway.providers.compatibility import capability_probes
from kessel_gateway.user_config import UserConfig


def test_codex_npm_shim_resolves_to_native_windows_binary(
    tmp_path: Path, monkeypatch
) -> None:
    npm_root = tmp_path / "npm"
    shim = npm_root / "codex.cmd"
    native = (
        npm_root
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    shim.write_text("@echo off\n", encoding="utf-8")
    native.write_bytes(b"")
    monkeypatch.setattr(executables.shutil, "which", lambda command: str(shim))
    monkeypatch.setattr(executables.platform, "machine", lambda: "AMD64")

    assert executables.resolve_executable(str(shim)) == str(native.resolve())


def test_provider_check_executes_resolved_binary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path))
    commands: list[list[str]] = []
    monkeypatch.setattr(
        health, "_resolve_executable", lambda command: "/native/codex"
    )

    def fake_run(command: list[str]) -> CompletedProcess[str]:
        commands.append(command)
        if "--version" in command:
            output = "codex-cli 1.2.3"
        elif "--help" in command:
            probe = next(
                probe
                for probe in capability_probes("codex")
                if list(probe.args) == command[1:]
            )
            output = " ".join(probe.required_markers)
        else:
            output = "Logged in"
        return CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(health, "_run", fake_run)

    result = health._check_provider(
        "codex", "Codex", "codex", ["login", "status"]
    )

    assert result.working is True
    assert result.executable == "/native/codex"
    assert commands == [
        ["/native/codex", "--version"],
        ["/native/codex", "exec", "--help"],
        ["/native/codex", "app-server", "--help"],
        ["/native/codex", "login", "status"],
    ]


def test_provider_check_rejects_missing_capability(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        health, "_resolve_executable", lambda command: "/native/codex"
    )

    def fake_run(command: list[str]) -> CompletedProcess[str]:
        output = "codex-cli 1.2.3" if "--version" in command else "--json"
        return CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(health, "_run", fake_run)

    result = health._check_provider(
        "codex", "Codex", "codex", ["login", "status"]
    )

    assert result.working is False
    assert result.compatible is False
    assert result.fix_command == "npm install -g @openai/codex@0.155.1"
    assert "missing required capabilities" in (result.detail or "")


def test_provider_checks_use_saved_command_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("KESSEL_CODEX_COMMAND", raising=False)
    monkeypatch.delenv("KESSEL_CLAUDE_COMMAND", raising=False)
    UserConfig(
        api_key="key",
        codex_command="saved-codex",
        claude_command="saved-claude",
    ).save()
    commands: list[tuple[str, str]] = []

    def fake_check(name, display_name, command, auth_args):
        commands.append((name, command))
        return health.ProviderHealth(name, display_name, False, False)

    monkeypatch.setattr(health, "_check_provider", fake_check)

    health.check_providers()

    assert commands == [
        ("claude", "saved-claude"),
        ("codex", "saved-codex"),
    ]


def test_provider_checks_prefer_environment_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("KESSEL_CODEX_COMMAND", "environment-codex")
    monkeypatch.setenv("KESSEL_CLAUDE_COMMAND", "environment-claude")
    commands: list[tuple[str, str]] = []

    def fake_check(name, display_name, command, auth_args):
        commands.append((name, command))
        return health.ProviderHealth(name, display_name, False, False)

    monkeypatch.setattr(health, "_check_provider", fake_check)

    health.check_providers()

    assert commands == [
        ("claude", "environment-claude"),
        ("codex", "environment-codex"),
    ]
