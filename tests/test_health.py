from pathlib import Path
from subprocess import CompletedProcess

from app.providers import health


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
    monkeypatch.setattr(health.shutil, "which", lambda command: str(shim))
    monkeypatch.setattr(health.platform, "machine", lambda: "AMD64")

    assert health._resolve_executable("codex") == str(native.resolve())


def test_provider_check_executes_resolved_binary(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        health, "_resolve_executable", lambda command: "/native/codex"
    )

    def fake_run(command: list[str]) -> CompletedProcess[str]:
        commands.append(command)
        output = "codex-cli 1.2.3" if "--version" in command else "Logged in"
        return CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(health, "_run", fake_run)

    result = health._check_provider(
        "codex", "Codex", "codex", ["login", "status"]
    )

    assert result.working is True
    assert result.executable == "/native/codex"
    assert commands == [
        ["/native/codex", "--version"],
        ["/native/codex", "login", "status"],
    ]
