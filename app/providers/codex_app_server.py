"""Persistent Codex App Server client with isolated concurrent turns."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from app.models import (
    ChatCompletionRequest,
    ProviderResult,
    ProviderStreamEvent,
    TokenUsage,
)
from app.process_security import ProcessGroupGuard, child_environment
from app.providers.base import uses_default_model
from app.rate_limits import RateLimitSnapshot
from app.runner import (
    ProcessError,
    ProcessNotFoundError,
    ProcessOutputLimitError,
    ProcessTimeoutError,
    hidden_process_options,
    provider_process_options,
    provider_error_from_message,
)


class CodexAppServer:
    """Own one App Server and dispatch concurrent turns to isolated queues."""

    def __init__(
        self,
        command: str,
        timeout_seconds: int,
        instructions_path: Path,
        disabled_features: Sequence[str],
        max_output_bytes: int = 1_048_576,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.instructions_path = instructions_path
        self.disabled_features = tuple(disabled_features)
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._restart_task: asyncio.Task | None = None
        self._process_guard: ProcessGroupGuard | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._thread_queues: dict[str, asyncio.Queue] = {}
        self._turn_queues: dict[tuple[str, str], asyncio.Queue] = {}
        self._stderr_tail = bytearray()
        self._stderr_retention_bytes = 65_536
        self._runtime_directory: tempfile.TemporaryDirectory | None = None
        self._instructions_text: str | None = None
        self._rate_limit: RateLimitSnapshot | None = None
        self._closing = False
        self._automatic_restart_used = False
        self._generation = 0

    async def start(self) -> None:
        restart = self._restart_task
        current = asyncio.current_task()
        if restart is not None and restart is not current and not restart.done():
            await asyncio.shield(restart)
        if self._process is not None and self._process.returncode is None:
            return

        async with self._start_lock:
            if self._process is not None and self._process.returncode is None:
                return
            if self._closing:
                raise ProcessError("Codex App Server is shutting down")

            await self._cleanup_runtime()
            executable = await asyncio.to_thread(shutil.which, self.command)
            if executable is None:
                raise ProcessNotFoundError(
                    f"command not found: {self.command}"
                )

            (
                self._runtime_directory,
                runtime_path,
                isolated_home,
                self._instructions_text,
            ) = await asyncio.to_thread(self._prepare_runtime)
            command = self._build_command(executable)
            env = child_environment({"CODEX_HOME": str(isolated_home)})

            self._generation += 1
            generation = self._generation
            process: asyncio.subprocess.Process | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(runtime_path),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **provider_process_options(),
                )
                self._process = process
                self._process_guard = ProcessGroupGuard.attach(process)
                self._reader_task = asyncio.create_task(
                    self._read_stdout(process, generation)
                )
                self._stderr_task = asyncio.create_task(
                    self._read_stderr(process)
                )
                await self._request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "kessel_local_api",
                            "title": "Kessel Local API",
                            "version": "0.2.0",
                        }
                    },
                )
                await self._notify("initialized", {})
            except BaseException:
                self._generation += 1
                if process is not None:
                    await self._stop_process(process)
                tasks = [
                    task
                    for task in (self._reader_task, self._stderr_task)
                    if task is not None and task is not asyncio.current_task()
                ]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                self._process = None
                self._reader_task = None
                self._stderr_task = None
                await self._cleanup_runtime()
                raise

    async def close(self) -> None:
        self._closing = True
        restart = self._restart_task
        if restart is not None and not restart.done():
            restart.cancel()
            await asyncio.gather(restart, return_exceptions=True)
        self._restart_task = None

        process = self._process
        self._process = None
        if process is not None:
            await self._stop_process(process)

        tasks = [
            task
            for task in (self._reader_task, self._stderr_task)
            if task is not None and task is not asyncio.current_task()
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        self._fail_inflight(ProcessError("Codex App Server shut down"))
        await self._cleanup_runtime()

    async def stream(
        self,
        request: ChatCompletionRequest,
        prompt: str,
        cwd: Path,
        schema: dict | None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        await self.start()
        if self._instructions_text is None:
            self._instructions_text = await asyncio.to_thread(
                self.instructions_path.read_text, encoding="utf-8"
            )
        assert self._runtime_directory is not None
        thread_params: dict[str, object] = {
            "cwd": self._runtime_directory.name,
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "ephemeral": True,
            "baseInstructions": self._instructions_text,
            "serviceName": "kessel",
            "serviceTier": request.service_tier,
            "config": {
                "skills": {"max_context_tokens": 1},
                "agents": {"enabled": False},
                "mcp_servers": {},
            },
        }
        if not uses_default_model("codex", request.model):
            thread_params["model"] = request.model
        response = await self._request("thread/start", thread_params)
        try:
            thread_id = response["thread"]["id"]
        except (KeyError, TypeError) as exc:
            raise ProcessError(
                "Codex App Server returned no thread id"
            ) from exc

        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._thread_queues[thread_id] = queue
        turn_params: dict[str, object] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "effort": request.reasoning_effort,
            "summary": "none",
            "serviceTierForTurn": request.service_tier,
        }
        if schema is not None:
            turn_params["outputSchema"] = schema

        full_text = ""
        usage: TokenUsage | None = None
        turn_id: str | None = None
        turn_completed = False
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        try:
            turn_response = await self._request("turn/start", turn_params)
            raw_turn = turn_response.get("turn", {})
            if isinstance(raw_turn, dict):
                raw_turn_id = raw_turn.get("id")
                if isinstance(raw_turn_id, str):
                    turn_id = raw_turn_id
                    self._turn_queues[(thread_id, turn_id)] = queue
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ProcessTimeoutError(
                        f"provider exceeded {self.timeout_seconds} second timeout"
                    )
                try:
                    message = await asyncio.wait_for(
                        queue.get(), timeout=remaining
                    )
                except asyncio.TimeoutError as exc:
                    raise ProcessTimeoutError(
                        f"provider exceeded {self.timeout_seconds} second timeout"
                    ) from exc
                method = message.get("method")
                params = message.get("params", {})
                if method == "item/agentMessage/delta":
                    delta = params.get("delta", "")
                    if isinstance(delta, str) and delta:
                        full_text += delta
                        if len(full_text.encode("utf-8")) > self.max_output_bytes:
                            raise ProcessOutputLimitError(
                                f"provider output exceeded {self.max_output_bytes} bytes"
                            )
                        yield ProviderStreamEvent(delta=delta)
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        text = item.get("text", "")
                        if isinstance(text, str) and text and not full_text:
                            full_text = text
                            if len(full_text.encode("utf-8")) > self.max_output_bytes:
                                raise ProcessOutputLimitError(
                                    f"provider output exceeded {self.max_output_bytes} bytes"
                                )
                            yield ProviderStreamEvent(delta=text)
                elif method == "thread/tokenUsage/updated":
                    usage = self._parse_usage(params.get("tokenUsage"))
                elif method in {"error", "server/error"}:
                    error = params.get("error", {})
                    message_text = error.get("message") or "Codex turn failed"
                    raise provider_error_from_message(message_text)
                elif method == "turn/completed":
                    turn = params.get("turn", {})
                    turn_completed = True
                    if turn.get("status") == "failed":
                        error = turn.get("error") or {}
                        message_text = error.get("message") or "Codex turn failed"
                        raise provider_error_from_message(message_text)
                    break
        finally:
            self._thread_queues.pop(thread_id, None)
            if turn_id is not None:
                self._turn_queues.pop((thread_id, turn_id), None)
            if turn_id is not None and not turn_completed:
                interrupt = asyncio.create_task(
                    self._request(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": turn_id},
                    )
                )
                try:
                    await asyncio.wait_for(
                        asyncio.shield(interrupt),
                        timeout=min(2, self.timeout_seconds),
                    )
                except asyncio.TimeoutError:
                    interrupt.cancel()
                    await asyncio.gather(interrupt, return_exceptions=True)
                except asyncio.CancelledError:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(interrupt),
                            timeout=min(2, self.timeout_seconds),
                        )
                    except asyncio.TimeoutError:
                        interrupt.cancel()
                        await asyncio.gather(
                            interrupt, return_exceptions=True
                        )
                    except ProcessError:
                        pass
                    raise
                except ProcessError:
                    pass

        if not full_text:
            raise ProcessError("Codex completed without an assistant message")
        yield ProviderStreamEvent(
            result=ProviderResult(text=full_text, model=request.model, usage=usage)
        )

    async def list_models(self) -> list[str]:
        await self.start()
        models: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, object] = {"limit": 100, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            response = await self._request("model/list", params)
            for item in response.get("data", []):
                if not isinstance(item, dict) or item.get("hidden") is True:
                    continue
                model_id = item.get("id") or item.get("model")
                if isinstance(model_id, str) and model_id:
                    models.append(model_id)
            cursor = response.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        return list(dict.fromkeys(models))

    async def rate_limit(self) -> RateLimitSnapshot | None:
        await self.start()
        response = await self._request("account/rateLimits/read", {})
        self._rate_limit = self._parse_rate_limit(response.get("rateLimits"))
        return self._rate_limit

    async def _request(self, method: str, params: dict) -> dict:
        await self._ensure_running()
        loop = asyncio.get_running_loop()
        request_id = self._next_id
        self._next_id += 1
        future = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"method": method, "id": request_id, "params": params}
            )
            response = await asyncio.wait_for(future, self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ProcessTimeoutError(
                f"Codex App Server did not answer {method}"
            ) from exc
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            raise ProcessError(error.get("message") or f"{method} failed")
        return response.get("result", {})

    async def _notify(self, method: str, params: dict) -> None:
        await self._write({"method": method, "params": params})

    async def _write(self, payload: dict) -> None:
        await self._ensure_running()
        assert self._process is not None and self._process.stdin is not None
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            try:
                self._process.stdin.write(data)
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ProcessError("Codex App Server closed stdin") from exc

    async def _ensure_running(self) -> None:
        if self._process is None or self._process.returncode is not None:
            raise ProcessError("Codex App Server is not running")

    async def _read_stdout(
        self, process: asyncio.subprocess.Process, generation: int
    ) -> None:
        assert process.stdout is not None
        error: ProcessError | None = None
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int) and request_id in self._pending:
                    future = self._pending.pop(request_id)
                    if not future.done():
                        future.set_result(message)
                    continue
                params = message.get("params", {})
                if message.get("method") == "account/rateLimits/updated":
                    raw_limits = (
                        params.get("rateLimits")
                        if isinstance(params, dict)
                        else None
                    )
                    self._rate_limit = self._parse_rate_limit(raw_limits)
                    continue
                if not isinstance(params, dict):
                    continue
                thread_id = params.get("threadId")
                turn_id = params.get("turnId")
                if not isinstance(turn_id, str):
                    raw_turn = params.get("turn")
                    if isinstance(raw_turn, dict):
                        turn_id = raw_turn.get("id")
                queue = None
                if isinstance(thread_id, str) and isinstance(turn_id, str):
                    queue = self._turn_queues.get((thread_id, turn_id))
                if queue is None and isinstance(thread_id, str):
                    queue = self._thread_queues.get(thread_id)
                if queue is not None:
                    try:
                        queue.put_nowait(message)
                    except asyncio.QueueFull as exc:
                        raise ProcessOutputLimitError(
                            "Codex App Server event queue exceeded its limit"
                        ) from exc
        except asyncio.CancelledError:
            raise
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            error = ProcessError(f"Codex App Server reader failed: {exc}")
        if self._closing or generation != self._generation:
            return
        error = error or ProcessError(
            "Codex App Server exited unexpectedly"
        )
        self._fail_inflight(error)
        if not self._automatic_restart_used:
            self._automatic_restart_used = True
            self._restart_task = asyncio.create_task(
                self._restart_once(process, generation)
            )

    async def _read_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        total_bytes = 0
        while chunk := await process.stderr.read(65_536):
            total_bytes += len(chunk)
            if total_bytes > self.max_output_bytes:
                error = ProcessOutputLimitError(
                    f"provider output exceeded {self.max_output_bytes} bytes"
                )
                self._fail_inflight(error)
                await self._stop_process(process)
                return
            self._stderr_tail.extend(chunk)
            excess = len(self._stderr_tail) - self._stderr_retention_bytes
            if excess > 0:
                del self._stderr_tail[:excess]

    async def _restart_once(
        self, process: asyncio.subprocess.Process, generation: int
    ) -> None:
        try:
            await self._stop_process(process)
            if self._closing or generation != self._generation:
                return
            await self.start()
        except (ProcessError, OSError):
            return

    def _fail_inflight(self, error: ProcessError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        message = {
            "method": "server/error",
            "params": {"error": {"message": str(error)}},
        }
        queues = {
            *self._thread_queues.values(),
            *self._turn_queues.values(),
        }
        for queue in queues:
            while queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            queue.put_nowait(message)

    def _prepare_runtime(
        self,
    ) -> tuple[tempfile.TemporaryDirectory, Path, Path, str]:
        temporary = tempfile.TemporaryDirectory(
            prefix="kessel-codex-server-"
        )
        try:
            runtime_path = Path(temporary.name)
            isolated_home = runtime_path / "home"
            isolated_home.mkdir()
            source_home = Path(
                os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
            )
            auth_path = source_home / "auth.json"
            if auth_path.exists():
                shutil.copy2(auth_path, isolated_home / "auth.json")
            instructions = self.instructions_path.read_text(encoding="utf-8")
            return temporary, runtime_path, isolated_home, instructions
        except BaseException:
            temporary.cleanup()
            raise

    def _build_command(self, executable: str) -> list[str]:
        command = [
            executable,
            "app-server",
            "--stdio",
            "--config",
            f'model_instructions_file={json.dumps(self.instructions_path.as_posix())}',
            "--config",
            "skills.max_context_tokens=1",
            "--config",
            "agents.enabled=false",
            "--config",
            "mcp_servers={}",
            "--config",
            'model_reasoning_summary="none"',
            "--config",
            'model_verbosity="low"',
        ]
        for feature in self.disabled_features:
            command.extend(["--disable", feature])
        return command

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        guard = self._process_guard
        if guard is not None and guard.handle is not None:
            guard.terminate()
            await process.wait()
            await guard.close()
            if guard is self._process_guard:
                self._process_guard = None
            return
        if process.returncode is not None:
            await process.wait()
            if guard is not None:
                await guard.close()
                if guard is self._process_guard:
                    self._process_guard = None
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
            if guard is not None:
                await guard.close()
                if guard is self._process_guard:
                    self._process_guard = None
            return
        except asyncio.TimeoutError:
            pass
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
            if guard is self._process_guard:
                self._process_guard = None

    async def _cleanup_runtime(self) -> None:
        if self._runtime_directory is not None:
            temporary = self._runtime_directory
            self._runtime_directory = None
            cleanup = asyncio.create_task(
                asyncio.to_thread(self._cleanup_temporary, temporary)
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    @staticmethod
    def _cleanup_temporary(temporary: tempfile.TemporaryDirectory) -> None:
        last_error: OSError | None = None
        for _ in range(20):
            try:
                temporary.cleanup()
                return
            except OSError as exc:
                last_error = exc
                if not Path(temporary.name).exists():
                    return
                time.sleep(0.1)
        if last_error is not None:
            raise last_error

    def _stderr_text(self) -> str:
        return bytes(self._stderr_tail).decode("utf-8", errors="replace").strip()

    @staticmethod
    def _parse_usage(raw_usage: object) -> TokenUsage | None:
        if not isinstance(raw_usage, dict):
            return None
        last = raw_usage.get("last")
        if not isinstance(last, dict):
            return None
        prompt_tokens = int(last.get("inputTokens", 0))
        completion_tokens = int(last.get("outputTokens", 0))
        cached_tokens = int(last.get("cachedInputTokens", 0))
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            prompt_tokens_details={"cached_tokens": cached_tokens},
        )

    @staticmethod
    def _parse_rate_limit(raw_limits: object) -> RateLimitSnapshot | None:
        if not isinstance(raw_limits, dict):
            return None
        windows = [
            window
            for key in ("primary", "secondary")
            if isinstance((window := raw_limits.get(key)), dict)
        ]
        if not windows:
            return None
        limiting_window = max(
            windows, key=lambda item: float(item.get("usedPercent", 0))
        )
        used_percent = float(limiting_window.get("usedPercent", 0))
        if raw_limits.get("rateLimitReachedType") is not None:
            used_percent = 100.0
        resets_at = limiting_window.get("resetsAt")
        if not isinstance(resets_at, int):
            resets_at = None
        return RateLimitSnapshot(
            remaining_percent=max(0.0, 100.0 - used_percent),
            resets_at=resets_at,
            limit_id=(
                str(raw_limits["limitId"])
                if raw_limits.get("limitId") is not None
                else None
            ),
        )
