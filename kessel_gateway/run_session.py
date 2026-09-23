"""Ownership metadata for temporary ``kessel run`` sessions."""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from kessel_gateway.user_config import state_directory


SESSION_MAX_AGE_SECONDS = 5.0


@dataclass(frozen=True)
class RunSession:
    token: str
    owner_pid: int
    provider: str
    heartbeat: float


class RunSessionStore:
    """Coordinate one foreground owner without treating durable services as owned."""

    @property
    def path(self) -> Path:
        return state_directory() / "run-session.json"

    def active(self) -> RunSession | None:
        session = self._read()
        if session is None:
            return None
        if (
            time.time() - session.heartbeat <= SESSION_MAX_AGE_SECONDS
            and _process_is_running(session.owner_pid)
        ):
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
            session = RunSession(
                token=str(payload["token"]),
                owner_pid=int(payload["owner_pid"]),
                provider=str(payload["provider"]),
                heartbeat=float(payload["heartbeat"]),
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
