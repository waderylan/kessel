"""Bounded asynchronous subprocess execution."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path


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


class ProcessExitError(ProcessError):
    def __init__(self, return_code: int, stderr: str) -> None:
        self.return_code = return_code
        self.stderr = stderr
        super().__init__(f"provider process exited with code {return_code}")


def provider_error_from_message(
    message: str, retry_after_seconds: int | None = None
) -> ProcessError:
    normalized = message.lower()
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


class ProcessRunner:
    """Run provider commands without a shell or command interpolation."""

    def __init__(self, timeout_seconds: int, max_output_bytes: int) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    async def run(
        self,
        command: list[str],
        stdin_text: str,
        cwd: Path,
    ) -> ProcessResult:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        executable = shutil.which(command[0])
        if executable is None:
            raise ProcessNotFoundError(f"command not found: {command[0]}")
        resolved_command = [executable, *command[1:]]
        try:
            process = await asyncio.create_subprocess_exec(
                *resolved_command,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ProcessNotFoundError(f"command not found: {command[0]}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin_text.encode("utf-8")),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProcessTimeoutError(
                f"provider exceeded {self.timeout_seconds} second timeout"
            ) from exc
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

        if len(stdout) + len(stderr) > self.max_output_bytes:
            raise ProcessOutputLimitError(
                f"provider output exceeded {self.max_output_bytes} bytes"
            )

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise ProcessExitError(process.returncode or 1, stderr_text.strip())
        return ProcessResult(stdout=stdout_text, stderr=stderr_text)

    async def stream_lines(
        self,
        command: list[str],
        stdin_text: str,
        cwd: Path,
    ) -> AsyncIterator[str]:
        """Yield newline-delimited stdout while keeping process bounds."""

        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        executable = shutil.which(command[0])
        if executable is None:
            raise ProcessNotFoundError(f"command not found: {command[0]}")
        process = await asyncio.create_subprocess_exec(
            executable,
            *command[1:],
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(stdin_text.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        stderr_task = asyncio.create_task(
            process.stderr.read(self.max_output_bytes + 1)
        )
        output_bytes = 0
        started_at = time.monotonic()
        try:
            while True:
                remaining = self.timeout_seconds - (time.monotonic() - started_at)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                line = await asyncio.wait_for(process.stdout.readline(), remaining)
                if not line:
                    break
                output_bytes += len(line)
                if output_bytes > self.max_output_bytes:
                    raise ProcessOutputLimitError(
                        f"provider output exceeded {self.max_output_bytes} bytes"
                    )
                yield line.decode("utf-8", errors="replace").rstrip("\r\n")

            remaining = self.timeout_seconds - (time.monotonic() - started_at)
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(process.wait(), remaining)
            stderr = await stderr_task
            if output_bytes + len(stderr) > self.max_output_bytes:
                raise ProcessOutputLimitError(
                    f"provider output exceeded {self.max_output_bytes} bytes"
                )
            if process.returncode != 0:
                raise ProcessExitError(
                    process.returncode or 1,
                    stderr.decode("utf-8", errors="replace").strip(),
                )
        except asyncio.TimeoutError as exc:
            raise ProcessTimeoutError(
                f"provider exceeded {self.timeout_seconds} second timeout"
            ) from exc
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if not stderr_task.done():
                stderr_task.cancel()
