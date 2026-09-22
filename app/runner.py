"""Bounded asynchronous subprocess execution."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from app.process_security import ProcessGroupGuard, child_environment


class ProcessError(RuntimeError):
    """Base error for provider subprocess failures."""


class ProcessNotFoundError(ProcessError):
    pass


class ProcessTimeoutError(ProcessError):
    pass


class ProcessOutputLimitError(ProcessError):
    pass


class ProviderRateLimitError(ProcessError):
    def __init__(self, message: str, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class ProviderBusyError(ProviderRateLimitError):
    """Raised when a provider concurrency slot is unavailable."""


class ProviderAuthenticationError(ProcessError):
    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(message)


class ProviderCompatibilityError(ProcessError):
    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"{provider} CLI is incompatible: {reason}")


class ProcessExitError(ProcessError):
    def __init__(
        self, return_code: int, stderr: str, stdout: str = ""
    ) -> None:
        self.return_code = return_code
        self.stderr = stderr
        self.stdout = stdout
        super().__init__(f"provider process exited with code {return_code}")


def provider_process_options() -> dict[str, object]:
    """Return isolated process options without opening a Windows console."""

    if os.name == "nt":
        return {
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
        }
    return {"start_new_session": True}


def hidden_process_options() -> dict[str, object]:
    """Prevent Windows helper processes from opening a console window."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def provider_error_from_message(
    message: str,
    retry_after_seconds: int | None = None,
    provider: str | None = None,
) -> ProcessError:
    normalized = message.lower()
    if provider and any(
        marker in normalized
        for marker in (
            "not logged in",
            "not authenticated",
            "authentication required",
            "please login",
            "please log in",
            "run /login",
        )
    ):
        return ProviderAuthenticationError(provider, message)
    if any(
        marker in normalized
        for marker in (
            "rate limit",
            "rate_limit",
            "usage limit",
            "quota exhausted",
            "too many requests",
        )
    ):
        return ProviderRateLimitError(message, retry_after_seconds)
    return ProcessError(message)


@dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str


@dataclass(frozen=True)
class _StreamCapture:
    retained: bytes
    total_bytes: int


class ProcessRunner:
    """Run provider commands asynchronously in independently killable groups."""

    def __init__(
        self,
        timeout_seconds: int,
        max_output_bytes: int,
        stderr_retention_bytes: int = 65_536,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.stderr_retention_bytes = stderr_retention_bytes
        self._processes: set[asyncio.subprocess.Process] = set()
        self._guards: dict[asyncio.subprocess.Process, ProcessGroupGuard] = {}

    @property
    def active_process_count(self) -> int:
        return len(self._processes)

    async def run(
        self,
        command: list[str],
        stdin_text: str,
        cwd: Path,
        env_overrides: dict[str, str] | None = None,
    ) -> ProcessResult:
        process = await self._spawn(command, cwd, env_overrides)
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_task = asyncio.create_task(
            self._drain_stream(process.stdout, self.max_output_bytes + 1)
        )
        stderr_task = asyncio.create_task(
            self._drain_stream(process.stderr, self.stderr_retention_bytes)
        )
        stdin_task = asyncio.create_task(self._feed_stdin(process, stdin_text))
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=self.timeout_seconds)
                await stdin_task
                stdout_capture, stderr_capture = await asyncio.gather(
                    stdout_task, stderr_task
                )
            except asyncio.TimeoutError as exc:
                await self._kill_process_group(process)
                raise ProcessTimeoutError(
                    f"provider exceeded {self.timeout_seconds} second timeout"
                ) from exc
            except asyncio.CancelledError:
                await self._kill_process_group(process)
                raise

            if (
                stdout_capture.total_bytes + stderr_capture.total_bytes
                > self.max_output_bytes
            ):
                raise ProcessOutputLimitError(
                    f"provider output exceeded {self.max_output_bytes} bytes"
                )

            stdout_text = stdout_capture.retained.decode(
                "utf-8", errors="replace"
            )
            stderr_text = stderr_capture.retained.decode(
                "utf-8", errors="replace"
            )
            if process.returncode != 0:
                raise ProcessExitError(
                    process.returncode or 1,
                    stderr_text.strip(),
                    stdout_text,
                )
            return ProcessResult(stdout=stdout_text, stderr=stderr_text)
        finally:
            await self._finish_tasks(stdin_task, stdout_task, stderr_task)
            if process.returncode is None:
                await self._kill_process_group(process)
            self._processes.discard(process)
            await self._release_guard(process)

    async def stream_lines(
        self,
        command: list[str],
        stdin_text: str,
        cwd: Path,
        env_overrides: dict[str, str] | None = None,
    ) -> AsyncIterator[str]:
        """Yield newline-delimited stdout while draining stderr concurrently."""

        process = await self._spawn(command, cwd, env_overrides)
        assert process.stdout is not None
        assert process.stderr is not None

        stderr_task = asyncio.create_task(
            self._drain_stream(process.stderr, self.stderr_retention_bytes)
        )
        stdin_task = asyncio.create_task(self._feed_stdin(process, stdin_text))
        stdout_bytes = 0
        started_at = time.monotonic()
        try:
            try:
                while True:
                    remaining = self.timeout_seconds - (
                        time.monotonic() - started_at
                    )
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    try:
                        line = await asyncio.wait_for(
                            process.stdout.readline(), timeout=remaining
                        )
                    except ValueError as exc:
                        raise ProcessOutputLimitError(
                            "provider emitted an oversized output line"
                        ) from exc
                    if not line:
                        break
                    stdout_bytes += len(line)
                    if stdout_bytes > self.max_output_bytes:
                        raise ProcessOutputLimitError(
                            f"provider output exceeded {self.max_output_bytes} bytes"
                        )
                    yield line.decode(
                        "utf-8", errors="replace"
                    ).rstrip("\r\n")

                remaining = self.timeout_seconds - (
                    time.monotonic() - started_at
                )
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait_for(process.wait(), timeout=remaining)
                await stdin_task
                stderr_capture = await stderr_task
                if stdout_bytes + stderr_capture.total_bytes > self.max_output_bytes:
                    raise ProcessOutputLimitError(
                        f"provider output exceeded {self.max_output_bytes} bytes"
                    )
                if process.returncode != 0:
                    stderr = stderr_capture.retained.decode(
                        "utf-8", errors="replace"
                    ).strip()
                    raise ProcessExitError(process.returncode or 1, stderr)
            except asyncio.TimeoutError as exc:
                await self._kill_process_group(process)
                raise ProcessTimeoutError(
                    f"provider exceeded {self.timeout_seconds} second timeout"
                ) from exc
            except asyncio.CancelledError:
                await self._kill_process_group(process)
                raise
        finally:
            if process.returncode is None:
                await self._kill_process_group(process)
            await self._finish_tasks(stdin_task, stderr_task)
            self._processes.discard(process)
            await self._release_guard(process)

    async def terminate_all(self) -> None:
        """Kill and reap every child process still owned by this runner."""

        await asyncio.gather(
            *(self._kill_process_group(process) for process in tuple(self._processes)),
            return_exceptions=True,
        )
        self._processes.clear()

    async def _spawn(
        self,
        command: list[str],
        cwd: Path,
        env_overrides: dict[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        executable = await asyncio.to_thread(shutil.which, command[0])
        if executable is None:
            raise ProcessNotFoundError(f"command not found: {command[0]}")

        env = child_environment(env_overrides)
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *command[1:],
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **provider_process_options(),
            )
        except FileNotFoundError as exc:
            raise ProcessNotFoundError(
                f"command not found: {command[0]}"
            ) from exc
        self._processes.add(process)
        self._guards[process] = ProcessGroupGuard.attach(process)
        return process

    @staticmethod
    async def _feed_stdin(
        process: asyncio.subprocess.Process, stdin_text: str
    ) -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(stdin_text.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    @staticmethod
    async def _drain_stream(
        stream: asyncio.StreamReader, retain_bytes: int
    ) -> _StreamCapture:
        retained = bytearray()
        total_bytes = 0
        while chunk := await stream.read(65_536):
            total_bytes += len(chunk)
            retained.extend(chunk)
            excess = len(retained) - retain_bytes
            if excess > 0:
                del retained[:excess]
        return _StreamCapture(bytes(retained), total_bytes)

    async def _kill_process_group(
        self, process: asyncio.subprocess.Process
    ) -> None:
        guard = self._guards.get(process)
        if guard is not None and guard.handle is not None:
            guard.terminate()
            await process.wait()
            await guard.close()
            self._guards.pop(process, None)
            return

        if process.returncode is not None:
            await process.wait()
            if guard is not None:
                await guard.close()
                self._guards.pop(process, None)
            return

        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **hidden_process_options(),
            )
            await killer.wait()
            if process.returncode is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()
        if guard is not None:
            await guard.close()
            self._guards.pop(process, None)

    async def _release_guard(self, process: asyncio.subprocess.Process) -> None:
        guard = self._guards.pop(process, None)
        if guard is not None:
            await guard.close()

    @staticmethod
    async def _finish_tasks(*tasks: asyncio.Task) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
