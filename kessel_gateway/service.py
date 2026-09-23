"""Install and control Kessel as a per-user background service."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - only available on Windows
    winreg = None  # type: ignore[assignment]

from kessel_gateway.user_config import UserConfig, state_directory


class ServiceError(RuntimeError):
    pass


class ServiceManager:
    """Small platform adapter for an unprivileged, login-scoped service."""

    def __init__(self, config: UserConfig) -> None:
        self.config = config

    @property
    def command(self) -> list[str]:
        return [sys.executable, "-m", "kessel_gateway.cli", "serve"]

    @property
    def log_directory(self) -> Path:
        return state_directory() / "logs"

    @property
    def stop_request_path(self) -> Path:
        return state_directory() / "stop.request"

    def is_running(self, timeout: float = 1.0) -> bool:
        request = urllib.request.Request(self.config.base_url + "/health")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    return False
                payload = json.loads(response.read().decode("utf-8"))
                providers = payload.get("providers", {})
                return isinstance(providers, dict) and payload.get(
                    "status"
                ) == "ok" and {
                    "claude",
                    "codex",
                }.issubset(providers)
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def is_registered(self) -> bool:
        """Return whether the per-user durable service has been installed."""

        if sys.platform == "win32":
            if winreg is None:
                return False
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                ) as key:
                    winreg.QueryValueEx(key, "Kessel")
                return True
            except OSError:
                return False
        if sys.platform == "darwin":
            return (
                Path.home() / "Library" / "LaunchAgents" / "dev.kessel.api.plist"
            ).exists()
        return (Path.home() / ".config" / "systemd" / "user" / "kessel.service").exists()

    def ensure_running(self) -> bool:
        """Install/start when needed. Return True when work was required."""

        if self.is_running():
            return False
        self.log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.log_directory.chmod(0o700)
        if sys.platform == "win32":
            self._install_windows()
        elif sys.platform == "darwin":
            self._install_macos()
        else:
            self._install_linux()
        return True

    def start(self) -> None:
        if self.is_running():
            return
        self.ensure_running()

    def stop(self) -> None:
        if sys.platform == "win32":
            self._stop_windows()
        elif sys.platform == "darwin":
            plist_path = Path.home() / "Library" / "LaunchAgents" / "dev.kessel.api.plist"
            self._run(
                [
                    "launchctl",
                    "bootout",
                    f"gui/{os.getuid()}",
                    str(plist_path),
                ],
                allow_failure=True,
            )
        else:
            self._run(
                ["systemctl", "--user", "stop", "kessel.service"],
                allow_failure=True,
            )

    def request_stop(self) -> None:
        path = self.stop_request_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text("stop\n", encoding="utf-8")

    def wait_until_stopped(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running():
                self.clear_stop_request()
                return True
            time.sleep(0.2)
        return False

    def wait_until_running(self, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_running():
                return True
            time.sleep(0.2)
        return False

    def _install_windows(self) -> None:
        if winreg is None:
            raise ServiceError("Windows startup registration is unavailable")
        command = subprocess.list2cmdline(self._windows_command())
        try:
            with winreg.CreateKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
            ) as key:
                winreg.SetValueEx(key, "Kessel", 0, winreg.REG_SZ, command)
        except OSError as exc:
            raise ServiceError(
                f"Could not register Kessel for the current user: {exc}"
            ) from exc

        self.clear_stop_request()
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
        try:
            subprocess.Popen(
                self._windows_command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creation_flags,
            )
        except OSError as exc:
            raise ServiceError(f"Could not start Kessel: {exc}") from exc

    def _windows_command(self) -> list[str]:
        executable = Path(sys.executable)
        pythonw = executable.with_name("pythonw.exe")
        return [
            str(pythonw if pythonw.exists() else executable),
            "-m",
            "kessel_gateway.cli",
            "serve",
        ]

    def _stop_windows(self) -> None:
        if not self.is_running():
            self.clear_stop_request()
            return
        self.request_stop()
        if not self.wait_until_stopped():
            raise ServiceError("Kessel did not stop within 15 seconds")

    def clear_stop_request(self) -> None:
        try:
            self.stop_request_path.unlink()
        except FileNotFoundError:
            pass

    def _install_linux(self) -> None:
        unit_directory = Path.home() / ".config" / "systemd" / "user"
        unit_directory.mkdir(parents=True, exist_ok=True)
        unit_path = unit_directory / "kessel.service"
        command = " ".join(self._systemd_quote(part) for part in self.command)
        unit = (
            "[Unit]\n"
            "Description=Kessel local API\n"
            "After=network.target\n\n"
            "[Service]\n"
            f"ExecStart={command}\n"
            "Restart=on-failure\n"
            "RestartSec=2\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        if not unit_path.exists() or unit_path.read_text(encoding="utf-8") != unit:
            unit_path.write_text(unit, encoding="utf-8")
            unit_path.chmod(0o600)
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(
            ["systemctl", "--user", "enable", "--now", "kessel.service"]
        )

    def _install_macos(self) -> None:
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist_path = agents / "dev.kessel.api.plist"
        payload = {
            "Label": "dev.kessel.api",
            "ProgramArguments": self.command,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": os.devnull,
            "StandardErrorPath": os.devnull,
        }
        serialized = plistlib.dumps(payload)
        changed = not plist_path.exists() or plist_path.read_bytes() != serialized
        if changed:
            self._run(
                [
                    "launchctl",
                    "bootout",
                    f"gui/{os.getuid()}",
                    str(plist_path),
                ],
                allow_failure=True,
            )
            plist_path.write_bytes(serialized)
            plist_path.chmod(0o600)
            self._run(
                [
                    "launchctl",
                    "bootstrap",
                    f"gui/{os.getuid()}",
                    str(plist_path),
                ]
            )
        else:
            result = self._run(
                [
                    "launchctl",
                    "kickstart",
                    f"gui/{os.getuid()}/dev.kessel.api",
                ],
                allow_failure=True,
            )
            if result.returncode != 0:
                self._run(
                    [
                        "launchctl",
                        "bootstrap",
                        f"gui/{os.getuid()}",
                        str(plist_path),
                    ]
                )

    @staticmethod
    def _systemd_quote(value: str) -> str:
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "$$")
            .replace("%", "%%")
        )
        return f'"{escaped}"'

    @staticmethod
    def _run(
        command: list[str], *, allow_failure: bool = False
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceError(f"Could not run {command[0]}: {exc}") from exc
        if result.returncode != 0 and not allow_failure:
            message = (result.stderr or result.stdout).strip()
            raise ServiceError(f"{' '.join(command[:3])} failed: {message}")
        return result
