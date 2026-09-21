"""Security helpers shared by provider subprocesses."""

from __future__ import annotations

import asyncio
import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass


_ENVIRONMENT_NAMES = {
    "APPDATA",
    "COMSPEC",
    "CODEX_HOME",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "CLAUDE_CONFIG_DIR",
    "LOCALAPPDATA",
    "LOGNAME",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
}


def child_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return a minimal child environment without unrelated service secrets."""

    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _ENVIRONMENT_NAMES or name.upper().startswith("LC_")
    }
    environment["NO_COLOR"] = "1"
    if overrides:
        environment.update(overrides)
    return environment


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _SYNCHRONIZE = 0x00100000
    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESSENTRY32W),
    ]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESSENTRY32W),
    ]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD

    def _descendant_pids(root_pid: int) -> list[int]:
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
            return []
        children: dict[int, list[int]] = {}
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            has_entry = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while has_entry:
                children.setdefault(int(entry.th32ParentProcessID), []).append(
                    int(entry.th32ProcessID)
                )
                has_entry = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)

        descendants: list[int] = []
        pending = list(children.get(root_pid, ()))
        while pending:
            pid = pending.pop()
            descendants.append(pid)
            pending.extend(children.get(pid, ()))
        return descendants

    def _terminate_descendants(root_pid: int) -> None:
        descendants = _descendant_pids(root_pid)
        for pid in reversed(descendants):
            process_handle = _kernel32.OpenProcess(
                _PROCESS_TERMINATE | _SYNCHRONIZE, False, pid
            )
            if process_handle:
                try:
                    _kernel32.TerminateProcess(process_handle, 1)
                    _kernel32.WaitForSingleObject(process_handle, 2000)
                finally:
                    _kernel32.CloseHandle(process_handle)


@dataclass
class ProcessGroupGuard:
    """Kill a complete Windows process tree when its root exits or is cancelled."""

    handle: int | None = None
    watcher: asyncio.Task | None = None
    root_pid: int | None = None

    @classmethod
    def attach(cls, process: asyncio.subprocess.Process) -> "ProcessGroupGuard":
        guard = cls(root_pid=process.pid)
        if os.name != "nt":
            return guard
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            guard.watcher = asyncio.create_task(guard._watch_root(process))
            return guard
        information = _EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        configured = _kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        process_handle = _kernel32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA,
            False,
            process.pid,
        )
        assigned = bool(
            configured
            and process_handle
            and _kernel32.AssignProcessToJobObject(handle, process_handle)
        )
        if process_handle:
            _kernel32.CloseHandle(process_handle)
        if not assigned:
            _kernel32.CloseHandle(handle)
            guard.watcher = asyncio.create_task(guard._watch_root(process))
            return guard
        guard.handle = int(handle)
        guard.watcher = asyncio.create_task(guard._watch_root(process))
        return guard

    async def _watch_root(self, process: asyncio.subprocess.Process) -> None:
        await process.wait()
        if self.handle is not None:
            self.terminate()
        elif os.name == "nt" and self.root_pid is not None:
            await asyncio.to_thread(_terminate_descendants, self.root_pid)

    def terminate(self) -> None:
        if self.handle is not None and os.name == "nt":
            descendants = (
                _descendant_pids(self.root_pid)
                if self.root_pid is not None
                else []
            )
            wait_handles = [
                handle
                for pid in descendants
                if (
                    handle := _kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
                )
            ]
            _kernel32.TerminateJobObject(self.handle, 1)
            for process_handle in wait_handles:
                _kernel32.WaitForSingleObject(process_handle, 2000)
                _kernel32.CloseHandle(process_handle)

    async def close(self) -> None:
        watcher = self.watcher
        self.watcher = None
        if watcher is not None and watcher is not asyncio.current_task():
            if not watcher.done():
                try:
                    await asyncio.wait_for(asyncio.shield(watcher), timeout=2)
                except asyncio.TimeoutError:
                    watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if self.handle is not None and os.name == "nt":
            _kernel32.CloseHandle(self.handle)
            self.handle = None
