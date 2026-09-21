from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.models import (
    ChatCompletionRequest,
    ProviderResult,
    ProviderStreamEvent,
)
from app.providers.base import ProviderAdapter
from app.providers.codex_app_server import CodexAppServer
from app.providers.registry import ProviderRegistry
from app.runner import ProcessRunner


def make_settings() -> Settings:
    return Settings(
        api_key=None,
        cors_origins=(),
        request_timeout_seconds=5,
        max_concurrent_requests=2,
        max_output_bytes=5_000_000,
        codex_command="codex",
        claude_command="claude",
        enforce_cli_versions=False,
        allow_unauthenticated=True,
    )


def request(text: str = "hello", backend: str = "fresh") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="default",
        backend=backend,
        messages=[{"role": "user", "content": text}],
    )


def write_fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake_cli.py"
    script.write_text(
        "import json, subprocess, sys, time\n"
        "mode, delay, marker, child = sys.argv[1:5]\n"
        "prompt = sys.stdin.read()\n"
        "if mode == 'stderr':\n"
        "    sys.stderr.buffer.write(b'x' * 2000000)\n"
        "    sys.stderr.buffer.flush()\n"
        "if mode == 'group' and 'quick' not in prompt:\n"
        "    subprocess.Popen([sys.executable, child, marker])\n"
        "    print(json.dumps({'delta': 'started'}), flush=True)\n"
        "    time.sleep(10)\n"
        "time.sleep(float(delay))\n"
        "print(json.dumps({'text': 'ok'}), flush=True)\n",
        encoding="utf-8",
    )
    child = tmp_path / "fake_child.py"
    child.write_text(
        "import pathlib, sys, time\n"
        "time.sleep(0.8)\n"
        "pathlib.Path(sys.argv[1]).write_text('orphaned')\n",
        encoding="utf-8",
    )
    return script


class ScriptProvider(ProviderAdapter):
    def __init__(
        self,
        name: str,
        runner: ProcessRunner,
        script: Path,
        *,
        mode: str = "normal",
        delay: float = 0.0,
        marker: Path | None = None,
    ) -> None:
        super().__init__(sys.executable, runner)
        self.name = name
        self.script = script
        self.mode = mode
        self.delay = delay
        self.marker = marker or script.with_name("unused-marker")

    def build_command(
        self, request: ChatCompletionRequest, cwd: Path | None = None
    ) -> list[str]:
        return [
            sys.executable,
            str(self.script),
            self.mode,
            str(self.delay),
            str(self.marker),
            str(self.script.with_name("fake_child.py")),
        ]

    def parse_output(self, output: str, requested_model: str) -> ProviderResult:
        payload = json.loads(output.splitlines()[-1])
        return ProviderResult(text=payload["text"], model=requested_model)

    async def stream(
        self, request: ChatCompletionRequest
    ):
        prompt = request.messages[-1].text()
        temporary = await asyncio.to_thread(
            tempfile.TemporaryDirectory, prefix="kessel-concurrency-test-"
        )
        line_stream = self.runner.stream_lines(
            self.build_command(request), prompt, Path(temporary.name)
        )
        full_text = ""
        try:
            async with aclosing(line_stream):
                async for line in line_stream:
                    payload = json.loads(line)
                    if "delta" in payload:
                        full_text += payload["delta"]
                        yield ProviderStreamEvent(delta=payload["delta"])
                    if "text" in payload:
                        full_text += payload["text"]
            yield ProviderStreamEvent(
                result=ProviderResult(text=full_text, model=request.model)
            )
        finally:
            await asyncio.to_thread(temporary.cleanup)


async def wait_for_processes(runner: ProcessRunner, count: int) -> None:
    deadline = asyncio.get_running_loop().time() + 2
    while runner.active_process_count != count:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"expected {count} active processes")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_requests_within_provider_limit_run_concurrently(
    tmp_path: Path,
) -> None:
    script = write_fake_cli(tmp_path)
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=5_000_000)
    provider = ScriptProvider("codex", runner, script, delay=0.4)
    registry = ProviderRegistry({"codex": provider}, 2, slot_wait_seconds=1)
    app = create_app(make_settings(), registry=registry)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        started = time.monotonic()
        responses = await asyncio.gather(
            *(
                client.post(
                    "/v1/codex/chat/completions",
                    json={
                        "model": "default",
                        "messages": [{"role": "user", "content": str(index)}],
                    },
                )
                for index in range(2)
            )
        )
        elapsed = time.monotonic() - started

    assert [response.status_code for response in responses] == [200, 200]
    assert elapsed < 0.75
    await registry.close()


@pytest.mark.asyncio
async def test_provider_limits_are_independent(tmp_path: Path) -> None:
    script = write_fake_cli(tmp_path)
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=5_000_000)
    registry = ProviderRegistry(
        {
            "codex": ScriptProvider("codex", runner, script, delay=0.4),
            "claude": ScriptProvider("claude", runner, script, delay=0.4),
        },
        {"codex": 1, "claude": 1},
        slot_wait_seconds=1,
    )
    app = create_app(make_settings(), registry=registry)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        started = time.monotonic()
        responses = await asyncio.gather(
            client.post(
                "/v1/codex/chat/completions",
                json={
                    "model": "default",
                    "messages": [{"role": "user", "content": "codex"}],
                },
            ),
            client.post(
                "/v1/claude/chat/completions",
                json={
                    "model": "default",
                    "messages": [{"role": "user", "content": "claude"}],
                },
            ),
        )
        elapsed = time.monotonic() - started

    assert [response.status_code for response in responses] == [200, 200]
    assert elapsed < 0.75
    await registry.close()


@pytest.mark.asyncio
async def test_health_remains_responsive_while_slots_are_busy(
    tmp_path: Path,
) -> None:
    script = write_fake_cli(tmp_path)
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=5_000_000)
    provider = ScriptProvider("codex", runner, script, delay=0.6)
    registry = ProviderRegistry({"codex": provider}, 2, slot_wait_seconds=1)
    app = create_app(make_settings(), registry=registry)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        requests = [
            asyncio.create_task(
                client.post(
                    "/v1/codex/chat/completions",
                    json={
                        "model": "default",
                        "messages": [{"role": "user", "content": str(index)}],
                    },
                )
            )
            for index in range(2)
        ]
        await wait_for_processes(runner, 2)
        started = time.monotonic()
        health = await client.get("/health")
        elapsed = time.monotonic() - started
        await asyncio.gather(*requests)

    assert health.status_code == 200
    assert elapsed < 0.3
    await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_name", "path", "payload", "anthropic"),
    [
        (
            "codex",
            "/v1/codex/chat/completions",
            {"model": "default", "messages": [{"role": "user", "content": "x"}]},
            False,
        ),
        (
            "claude",
            "/v1/messages",
            {
                "model": "default",
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "x"}],
            },
            True,
        ),
    ],
)
async def test_provider_queue_timeout_returns_native_429(
    tmp_path: Path,
    provider_name: str,
    path: str,
    payload: dict,
    anthropic: bool,
) -> None:
    script = write_fake_cli(tmp_path)
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=5_000_000)
    provider = ScriptProvider(provider_name, runner, script, delay=0.5)
    registry = ProviderRegistry(
        {provider_name: provider}, 1, slot_wait_seconds=0.1
    )
    app = create_app(make_settings(), registry=registry)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        first = asyncio.create_task(client.post(path, json=payload))
        await wait_for_processes(runner, 1)
        response = await client.post(path, json=payload)
        await first

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    if anthropic:
        assert response.json()["error"]["type"] == "rate_limit_error"
    else:
        assert response.json()["error"]["code"] == "provider_busy"
    await registry.close()


class ConcurrentFakeAppServer(CodexAppServer):
    def __init__(self, instructions_path: Path) -> None:
        super().__init__("codex", 2, instructions_path, ())
        self._runtime_directory = tempfile.TemporaryDirectory(
            prefix="kessel-concurrent-app-server-"
        )
        self._fake_stdout = asyncio.StreamReader()
        self._counter = 0
        self._emitters: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._reader_task is None:
            self._generation = 1
            fake_process = SimpleNamespace(stdout=self._fake_stdout)
            self._reader_task = asyncio.create_task(
                self._read_stdout(fake_process, self._generation)
            )

    async def _request(self, method: str, params: dict) -> dict:
        if method == "thread/start":
            self._counter += 1
            return {"thread": {"id": f"thread-{self._counter}"}}
        if method == "turn/start":
            thread_id = params["threadId"]
            turn_id = thread_id.replace("thread", "turn")
            text = params["input"][0]["text"]

            async def emit() -> None:
                await asyncio.sleep(0.01 if thread_id.endswith("1") else 0)
                for message in (
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "delta": text,
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "turn": {"id": turn_id, "status": "completed"},
                        },
                    },
                ):
                    self._fake_stdout.feed_data(
                        (json.dumps(message) + "\n").encode()
                    )
                    await asyncio.sleep(0)

            self._emitters.append(asyncio.create_task(emit()))
            return {"turn": {"id": turn_id}}
        return {}

    async def finish(self) -> None:
        await asyncio.gather(*self._emitters)
        self._closing = True
        self._fake_stdout.feed_eof()
        if self._reader_task is not None:
            await self._reader_task
        if self._runtime_directory is not None:
            self._runtime_directory.cleanup()
            self._runtime_directory = None


@pytest.mark.asyncio
async def test_concurrent_warm_turns_do_not_cross_streams(tmp_path: Path) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = ConcurrentFakeAppServer(instructions)

    async def collect(text: str) -> str:
        events = [
            event
            async for event in server.stream(
                request(text, backend="warm"), text, tmp_path, None
            )
        ]
        return "".join(event.delta for event in events)

    first, second = await asyncio.gather(collect("FIRST"), collect("SECOND"))

    assert first == "FIRST"
    assert second == "SECOND"
    await server.finish()


@pytest.mark.asyncio
async def test_large_stderr_is_drained_without_deadlock(tmp_path: Path) -> None:
    script = write_fake_cli(tmp_path)
    runner = ProcessRunner(
        timeout_seconds=5,
        max_output_bytes=5_000_000,
        stderr_retention_bytes=32_768,
    )

    result = await runner.run(
        [
            sys.executable,
            str(script),
            "stderr",
            "0",
            str(tmp_path / "unused"),
            str(tmp_path / "fake_child.py"),
        ],
        "input",
        tmp_path,
    )

    assert json.loads(result.stdout)["text"] == "ok"
    assert len(result.stderr.encode()) == 32_768


@pytest.mark.asyncio
async def test_stream_cancellation_kills_group_and_releases_slot(
    tmp_path: Path,
) -> None:
    script = write_fake_cli(tmp_path)
    marker = tmp_path / "orphaned.txt"
    runner = ProcessRunner(timeout_seconds=15, max_output_bytes=5_000_000)
    provider = ScriptProvider(
        "codex", runner, script, mode="group", marker=marker
    )
    registry = ProviderRegistry({"codex": provider}, 1, slot_wait_seconds=1)
    stream = registry.stream("codex", request("long"))

    assert (await anext(stream)).delta == "started"
    await stream.aclose()
    result = await asyncio.wait_for(
        registry.complete("codex", request("quick")), timeout=2
    )
    await asyncio.sleep(1)

    assert result.text == "ok"
    assert runner.active_process_count == 0
    assert not marker.exists()
    await registry.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_requests_and_reaps_process_groups(
    tmp_path: Path,
) -> None:
    script = write_fake_cli(tmp_path)
    marker = tmp_path / "shutdown-orphan.txt"
    runner = ProcessRunner(timeout_seconds=15, max_output_bytes=5_000_000)
    provider = ScriptProvider(
        "codex", runner, script, mode="group", marker=marker
    )
    registry = ProviderRegistry(
        {"codex": provider},
        1,
        slot_wait_seconds=1,
        shutdown_grace_seconds=0.1,
    )
    task = asyncio.create_task(registry.complete("codex", request("long")))
    await wait_for_processes(runner, 1)

    await registry.close()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(1)

    assert runner.active_process_count == 0
    assert not marker.exists()
