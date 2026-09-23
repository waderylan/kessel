import subprocess
from contextlib import nullcontext
from pathlib import Path

from kessel_gateway import service
from kessel_gateway.service import ServiceManager
from kessel_gateway.user_config import UserConfig


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
