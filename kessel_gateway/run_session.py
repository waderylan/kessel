"""Ownership metadata for temporary ``kessel run`` sessions."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from kessel_gateway.user_config import state_directory


# Heartbeat tolerance used only when the owner's start time is unavailable.
# With a known start time, the PID and start-time checks decide liveness, so
# an arbitrarily long laptop sleep never ends a live session.
SESSION_MAX_AGE_SECONDS = 60.0


@dataclass(frozen=True)
class RunSession:
    token: str
    owner_pid: int
    provider: str
    heartbeat: float
    owner_start_time: float | None = None


class RunSessionStore:
    """Coordinate one foreground owner without treating durable services as owned."""

    @property
    def path(self) -> Path:
        return state_directory() / "run-session.json"

    def active(self) -> RunSession | None:
        session = self._read()
        if session is None:
            return None
        if _session_is_active(session):
            return session
        self.release(session.token)
        return None

    def claim(self, provider: str) -> RunSession | None:
        """Claim ownership, returning ``None`` when another owner is active."""

        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            path.parent.chmod(0o700)
        for _ in range(2):
            if self.active() is not None:
                return None
            session = RunSession(
                token=secrets.token_urlsafe(24),
                owner_pid=os.getpid(),
                provider=provider,
                heartbeat=time.time(),
                owner_start_time=_process_start_time(os.getpid()),
            )
            try:
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                if self._read() is not None:
                    return None
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(session), handle, separators=(",", ":"))
                handle.write("\n")
            return session
        return None

    def heartbeat(self, session: RunSession) -> RunSession:
        current = self._read()
        if current is None or current.token != session.token:
            raise RuntimeError("Kessel run session ownership was lost")
        updated = RunSession(
            token=session.token,
            owner_pid=session.owner_pid,
            provider=session.provider,
            heartbeat=time.time(),
            owner_start_time=session.owner_start_time,
        )
        temporary = self.path.with_name(f".{self.path.name}.{session.token}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(asdict(updated), handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return updated

    def release(self, token: str) -> None:
        current = self._read()
        if current is None or current.token != token:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _read(self) -> RunSession | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            owner_start_time = payload.get("owner_start_time")
            session = RunSession(
                token=str(payload["token"]),
                owner_pid=int(payload["owner_pid"]),
                provider=str(payload["provider"]),
                heartbeat=float(payload["heartbeat"]),
                owner_start_time=(
                    float(owner_start_time) if owner_start_time is not None else None
                ),
            )
            if session.provider not in {"codex", "claude"}:
                return None
            return session
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError):
            return None


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        exit_code = wintypes.DWORD()
        try:
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
                exit_code.value == still_active
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _session_is_active(session: RunSession) -> bool:
    """Return whether ``session``'s owner is still alive and unreplaced.

    Liveness is decided primarily by the owner PID (and its start time, to
    guard against PID reuse), which survives an arbitrarily long laptop
    sleep. The heartbeat age is only a secondary check with a wide tolerance
    so scheduling jitter around sleep/wake never misreports a live session.
    """

    if not _process_is_running(session.owner_pid):
        return False
    if session.owner_start_time is not None:
        current_start_time = _process_start_time(session.owner_pid)
        if current_start_time is not None:
            # A matching start time proves the original owner is alive, so an
            # old heartbeat only means the machine slept.
            return abs(current_start_time - session.owner_start_time) <= 2
    return time.time() - session.heartbeat <= SESSION_MAX_AGE_SECONDS


def _process_start_time(pid: int) -> float | None:
    """Best-effort process start time, in epoch seconds, or ``None``."""

    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_start_time(pid)
    if sys.platform == "darwin":
        return _macos_process_start_time(pid)
    return _linux_process_start_time(pid)


def _windows_process_start_time(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        # FILETIME is 100-ns intervals since 1601-01-01; convert to a Unix
        # epoch timestamp.
        epoch_difference_100ns = 116444736000000000
        return (ticks - epoch_difference_100ns) / 10_000_000
    finally:
        kernel32.CloseHandle(handle)


def _linux_process_start_time(pid: int) -> float | None:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # The second field (comm) is parenthesized and may itself contain
        # spaces or parentheses, so split on the last ')' before reading the
        # remaining space-separated fields.
        after_comm = stat_text.rsplit(")", 1)[1].split()
        starttime_ticks = int(after_comm[19])  # field 22, 3rd after comm
        clock_ticks_per_second = os.sysconf("SC_CLK_TCK")
        boot_time = None
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime"):
                boot_time = int(line.split()[1])
                break
        if boot_time is None:
            return None
        return boot_time + starttime_ticks / clock_ticks_per_second
    except (OSError, IndexError, ValueError):
        return None


def _macos_process_start_time(pid: int) -> float | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    if not text:
        return None
    try:
        parsed = time.strptime(text, "%a %b %d %H:%M:%S %Y")
        return time.mktime(parsed)
    except ValueError:
        return None
