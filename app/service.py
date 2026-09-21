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

from app.user_config import UserConfig, state_directory


class ServiceError(RuntimeError):
    pass


class ServiceManager:
    """Small platform adapter for an unprivileged, login-scoped service."""

    def __init__(self, config: UserConfig) -> None:
        self.config = config

    @property
    def command(self) -> list[str]:
        return [sys.executable, "-m", "app.cli", "serve"]

    @property
    def log_directory(self) -> Path:
        return state_directory() / "logs"

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
            self._run(["schtasks", "/End", "/TN", "Kessel"], allow_failure=True)
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

    def wait_until_running(self, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_running():
                return True
            time.sleep(0.2)
        return False

    def _install_windows(self) -> None:
        task_command = subprocess.list2cmdline(self.command)
        result = self._run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/TN",
                "Kessel",
                "/SC",
                "ONLOGON",
                "/RL",
                "LIMITED",
                "/TR",
                task_command,
            ],
            allow_failure=True,
        )
        if result.returncode != 0:
            raise ServiceError(
                "Could not install the Kessel scheduled task: "
                + (result.stderr or result.stdout).strip()
            )
        self._run(["schtasks", "/Run", "/TN", "Kessel"])

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
