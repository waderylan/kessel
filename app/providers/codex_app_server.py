"""Persistent Codex App Server client with ephemeral per-request threads."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from app.models import (
    ChatCompletionRequest,
    ProviderResult,
    ProviderStreamEvent,
    TokenUsage,
)
from app.providers.base import uses_default_model
from app.rate_limits import RateLimitSnapshot
from app.runner import (
    ProcessError,
    ProcessNotFoundError,
    ProcessTimeoutError,
    provider_error_from_message,
)


class CodexAppServer:
    """Own one app-server process and isolate calls in new ephemeral threads."""

    def __init__(
        self,
        command: str,
        timeout_seconds: int,
        instructions_path: Path,
        disabled_features: Sequence[str],
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.instructions_path = instructions_path
        self.disabled_features = tuple(disabled_features)
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._thread_queues: dict[str, asyncio.Queue] = {}
        self._stderr_tail = ""
        self._runtime_directory: tempfile.TemporaryDirectory | None = None
        self._rate_limit: RateLimitSnapshot | None = None

    async def start(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        async with self._start_lock:
            if self._process is not None and self._process.returncode is None:
                return
            executable = shutil.which(self.command)
            if executable is None:
                raise ProcessNotFoundError(f"command not found: {self.command}")
            self._runtime_directory = tempfile.TemporaryDirectory(
                prefix="kessel-codex-server-"
            )
            runtime_path = Path(self._runtime_directory.name)
            isolated_home = runtime_path / "home"
            isolated_home.mkdir()
            source_home = Path(
                os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
            )
            auth_path = source_home / "auth.json"
            if auth_path.exists():
                shutil.copy2(auth_path, isolated_home / "auth.json")

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
            env = os.environ.copy()
            env["NO_COLOR"] = "1"
            env["CODEX_HOME"] = str(isolated_home)
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(runtime_path),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
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

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        tasks = [
            task
            for task in (self._reader_task, self._stderr_task)
            if task is not None
        ]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        if self._runtime_directory is not None:
            self._runtime_directory.cleanup()
            self._runtime_directory = None

    async def stream(
        self,
        request: ChatCompletionRequest,
        prompt: str,
        cwd: Path,
        schema: dict | None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        await self.start()
        thread_params: dict[str, object] = {
            "cwd": str(Path(self._runtime_directory.name).resolve()),
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "ephemeral": True,
            "baseInstructions": self.instructions_path.read_text(encoding="utf-8"),
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
            raise ProcessError("Codex App Server returned no thread id") from exc

        queue: asyncio.Queue = asyncio.Queue()
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
        try:
            turn_response = await self._request("turn/start", turn_params)
            raw_turn = turn_response.get("turn", {})
            if isinstance(raw_turn, dict):
                raw_turn_id = raw_turn.get("id")
                if isinstance(raw_turn_id, str):
                    turn_id = raw_turn_id
            while True:
                try:
                    message = await asyncio.wait_for(
                        queue.get(), timeout=self.timeout_seconds
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
                        yield ProviderStreamEvent(delta=delta)
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        text = item.get("text", "")
                        if isinstance(text, str) and text and not full_text:
                            full_text = text
                            yield ProviderStreamEvent(delta=text)
                elif method == "thread/tokenUsage/updated":
                    usage = self._parse_usage(params.get("tokenUsage"))
                elif method == "error":
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
            if turn_id is not None and not turn_completed:
                interrupt = asyncio.create_task(
                    self._request(
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": turn_id},
                    )
                )
                try:
                    await asyncio.shield(interrupt)
                except asyncio.CancelledError:
                    await interrupt
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
        await self._write({"method": method, "id": request_id, "params": params})
        try:
            response = await asyncio.wait_for(future, self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise ProcessTimeoutError(
                f"Codex App Server did not answer {method}"
            ) from exc
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
            self._process.stdin.write(data)
            await self._process.stdin.drain()

    async def _ensure_running(self) -> None:
        if self._process is None or self._process.returncode is not None:
            detail = f": {self._stderr_tail}" if self._stderr_tail else ""
            raise ProcessError(f"Codex App Server is not running{detail}")

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
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
                    raw_limits = params.get("rateLimits") if isinstance(params, dict) else None
                    self._rate_limit = self._parse_rate_limit(raw_limits)
                    continue
                thread_id = params.get("threadId") if isinstance(params, dict) else None
                queue = self._thread_queues.get(thread_id)
                if queue is not None:
                    queue.put_nowait(message)
        finally:
            error = ProcessError("Codex App Server exited unexpectedly")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while chunk := await self._process.stderr.read(4096):
            text = chunk.decode("utf-8", errors="replace")
            self._stderr_tail = (self._stderr_tail + text)[-4000:]

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
        limiting_window = max(windows, key=lambda item: float(item.get("usedPercent", 0)))
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
