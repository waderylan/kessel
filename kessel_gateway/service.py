"""Install and control Kessel as a per-user background service."""

from __future__ import annotations

import json
import os
import plistlib
import socket
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


# Proxy and CA-bundle variables, checked in both upper and lower case, that
# durable services need to reach the network the same way the interactive
# shell that ran `kessel start` could. Never add credential or Kessel
# variables here.
_CAPTURED_PROXY_VARIABLE_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
)


def _captured_environment() -> dict[str, str]:
    """Environment to embed in the durable service's launch configuration.

    launchd and systemd start services with a minimal environment, so an
    npm-installed ``codex``/``claude`` (a ``#!/usr/bin/env node`` script)
    cannot be found without capturing the current ``PATH``. Proxy and CA
    variables are captured the same way. This never captures API keys or
    other Kessel variables.
    """

    captured: dict[str, str] = {}
    path_value = os.environ.get("PATH")
    if path_value:
        captured["PATH"] = path_value
    for name in _CAPTURED_PROXY_VARIABLE_NAMES:
        for candidate in (name, name.lower()):
            value = os.environ.get(candidate)
            if value is not None:
                captured[candidate] = value
    return captured


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

    def health(self, timeout: float = 1.0) -> dict | None:
        """Return the parsed ``/health`` payload, or ``None`` when unreachable.

        ``/health`` answers 200 whenever Kessel is alive and 503 when no
        provider is available; both carry a JSON body, so a non-200 status
        is not treated as a connection failure here.
        """

        request = urllib.request.Request(self.config.base_url + "/health")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
        except (OSError, urllib.error.URLError):
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def is_running(self, timeout: float = 1.0) -> bool:
        """Return whether a Kessel server is alive, whatever its status.

        This is a liveness check, not a readiness check: a server reporting
        ``status: degraded`` (no provider currently available) still counts
        as running so ``kessel status`` and ``kessel stop`` work on it.
        """

        payload = self.health(timeout=timeout)
        return payload is not None and payload.get("service") == "kessel"

    def port_conflict(self) -> bool:
        """Return whether another program is listening on Kessel's address."""

        if self.is_running():
            return False
        try:
            with socket.create_connection(
                (self.config.host, self.config.port), timeout=0.5
            ):
                return True
        except OSError:
            return False

    def port_conflict_message(self) -> str:
        return (
            f"Port {self.config.port} on {self.config.host} is already used by "
            "another program. Stop that program, or choose another port with: "
            "kessel setup --port <port>"
        )

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

    def uninstall(self) -> None:
        """Stop Kessel and remove the durable service registration.

        Idempotent: safe to call whether or not the service is currently
        installed or running. Configuration, the API key, and logs are left
        in place.
        """

        if self.is_running():
            self.stop()
            self.wait_until_stopped()
        if sys.platform == "win32":
            self._uninstall_windows()
        elif sys.platform == "darwin":
            self._uninstall_macos()
        else:
            self._uninstall_linux()

    def _uninstall_windows(self) -> None:
        if winreg is None:
            return
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_ALL_ACCESS,
            ) as key:
                winreg.DeleteValue(key, "Kessel")
        except OSError:
            pass

    def _uninstall_macos(self) -> None:
        plist_path = Path.home() / "Library" / "LaunchAgents" / "dev.kessel.api.plist"
        self._run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
            allow_failure=True,
        )
        try:
            plist_path.unlink()
        except FileNotFoundError:
            pass

    def _uninstall_linux(self) -> None:
        unit_path = Path.home() / ".config" / "systemd" / "user" / "kessel.service"
        self._run(
            ["systemctl", "--user", "disable", "--now", "kessel.service"],
            allow_failure=True,
        )
        try:
            unit_path.unlink()
        except FileNotFoundError:
            pass
        self._run(["systemctl", "--user", "daemon-reload"], allow_failure=True)

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
        environment_lines = "".join(
            f"Environment={self._systemd_quote(f'{name}={value}')}\n"
            for name, value in _captured_environment().items()
        )
        unit = (
            "[Unit]\n"
            "Description=Kessel local API\n"
            "After=network.target\n\n"
            "[Service]\n"
            f"ExecStart={command}\n"
            f"{environment_lines}"
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
            "StandardOutPath": str(self.log_directory / "kessel-service.out.log"),
            "StandardErrorPath": str(self.log_directory / "kessel-service.err.log"),
            "EnvironmentVariables": _captured_environment(),
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
