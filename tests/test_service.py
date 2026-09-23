import io
import json
import plistlib
import subprocess
import urllib.error
from contextlib import nullcontext
from pathlib import Path

from kessel_gateway import service
from kessel_gateway.service import ServiceManager
from kessel_gateway.user_config import UserConfig


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    REG_SZ = 1

    def __init__(self) -> None:
        self.created: tuple[object, str] | None = None
        self.value: tuple[object, str, int, int, str] | None = None

    def CreateKey(self, root: object, path: str):
        self.created = (root, path)
        return nullcontext("key")

    def SetValueEx(
        self, key: object, name: str, reserved: int, kind: int, value: str
    ) -> None:
        self.value = (key, name, reserved, kind, value)

    def OpenKey(self, root: object, path: str):
        self.created = (root, path)
        return nullcontext("key")

    def QueryValueEx(self, key: object, name: str) -> tuple[str, int]:
        if name != "Kessel":
            raise FileNotFoundError(name)
        return "pythonw -m kessel_gateway.cli serve", self.REG_SZ


def test_windows_install_uses_current_user_startup_without_schtasks(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    registry = FakeRegistry()
    spawned: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(service, "winreg", registry)
    monkeypatch.setattr(
        subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0x200,
        raising=False,
    )
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda command, **kwargs: spawned.append((command, kwargs)),
    )
    manager = ServiceManager(UserConfig(api_key="secret"))
    monkeypatch.setattr(manager, "_windows_command", lambda: ["pythonw", "serve"])

    manager._install_windows()

    assert registry.created == (
        registry.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
    )
    assert registry.value == (
        "key",
        "Kessel",
        0,
        registry.REG_SZ,
        "pythonw serve",
    )
    assert spawned[0][0] == ["pythonw", "serve"]
    assert "shell" not in spawned[0][1]
    assert spawned[0][1]["stdin"] is subprocess.DEVNULL
    assert spawned[0][1]["stdout"] is subprocess.DEVNULL
    assert spawned[0][1]["stderr"] is subprocess.DEVNULL


def test_windows_registration_detection(monkeypatch) -> None:
    registry = FakeRegistry()
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service, "winreg", registry)
    manager = ServiceManager(UserConfig(api_key="secret"))

    assert manager.is_registered()


class FakeRegistryWithDelete(FakeRegistry):
    KEY_ALL_ACCESS = 0xF003F

    def __init__(self) -> None:
        super().__init__()
        self.deleted: str | None = None

    def OpenKey(self, root: object, path: str, reserved: int = 0, access: int = 0):
        self.created = (root, path)
        return nullcontext("key")

    def DeleteValue(self, key: object, name: str) -> None:
        self.deleted = name


def test_windows_uninstall_removes_registry_value(monkeypatch) -> None:
    registry = FakeRegistryWithDelete()
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service, "winreg", registry)
    manager = ServiceManager(UserConfig(api_key="secret"))
    monkeypatch.setattr(manager, "is_running", lambda: False)

    manager.uninstall()

    assert registry.deleted == "Kessel"


def test_windows_uninstall_is_idempotent_when_value_missing(monkeypatch) -> None:
    class MissingValueRegistry(FakeRegistryWithDelete):
        def OpenKey(self, root: object, path: str, reserved: int = 0, access: int = 0):
            raise OSError("not found")

    registry = MissingValueRegistry()
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service, "winreg", registry)
    manager = ServiceManager(UserConfig(api_key="secret"))
    monkeypatch.setattr(manager, "is_running", lambda: False)

    manager.uninstall()  # must not raise


def test_health_returns_payload_for_200(monkeypatch) -> None:
    payload = {
        "service": "kessel",
        "version": "1.0",
        "status": "ok",
        "providers": {},
    }
    monkeypatch.setattr(
        service.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(200, json.dumps(payload).encode()),
    )
    manager = ServiceManager(UserConfig(api_key="secret"))

    assert manager.health() == payload
    assert manager.is_running() is True


def test_health_parses_503_body_instead_of_treating_it_as_down(monkeypatch) -> None:
    payload = {
        "service": "kessel",
        "version": "1.0",
        "status": "degraded",
        "providers": {"codex": {"available": False}},
    }
    body = json.dumps(payload).encode()

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 503, "Service Unavailable", None, io.BytesIO(body)
        )

    monkeypatch.setattr(service.urllib.request, "urlopen", fake_urlopen)
    manager = ServiceManager(UserConfig(api_key="secret"))

    assert manager.health() == payload
    assert manager.is_running() is True


def test_is_running_false_when_service_field_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        service.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(200, b'{"status": "ok"}'),
    )
    manager = ServiceManager(UserConfig(api_key="secret"))

    assert manager.is_running() is False


def test_is_running_false_on_connection_error(monkeypatch) -> None:
    def fake_urlopen(request, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(service.urllib.request, "urlopen", fake_urlopen)
    manager = ServiceManager(UserConfig(api_key="secret"))

    assert manager.is_running() is False


def test_captured_environment_excludes_secrets(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/custom/node/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("http_proxy", "http://lower.proxy.example:8080")
    monkeypatch.setenv("KESSEL_API_KEY", "should-not-appear")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-appear")
    monkeypatch.setenv("KESSEL_CONFIG_DIR", "/should/not/appear")

    captured = service._captured_environment()

    assert captured["PATH"] == "/usr/bin:/custom/node/bin"
    assert captured["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert captured["http_proxy"] == "http://lower.proxy.example:8080"
    assert "KESSEL_API_KEY" not in captured
    assert "OPENAI_API_KEY" not in captured
    assert not any(name.upper().startswith("KESSEL_") for name in captured)
    assert not any("API_KEY" in name.upper() for name in captured)


def test_linux_unit_includes_captured_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/opt/node/bin")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    manager = ServiceManager(UserConfig(api_key="secret"))
    monkeypatch.setattr(manager, "_run", lambda *args, **kwargs: None)

    manager._install_linux()

    unit_text = (
        home / ".config" / "systemd" / "user" / "kessel.service"
    ).read_text(encoding="utf-8")
    assert 'Environment="PATH=/usr/bin:/opt/node/bin"' in unit_text


def test_macos_plist_includes_environment_and_log_paths(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/opt/node/bin")
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    manager = ServiceManager(UserConfig(api_key="secret"))
    monkeypatch.setattr(
        manager, "_run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0)
    )

    manager._install_macos()

    plist_path = home / "Library" / "LaunchAgents" / "dev.kessel.api.plist"
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["EnvironmentVariables"]["PATH"] == "/usr/bin:/opt/node/bin"
    assert str(manager.log_directory) in payload["StandardOutPath"]
    assert str(manager.log_directory) in payload["StandardErrorPath"]
    assert payload["StandardOutPath"] != payload["StandardErrorPath"]
