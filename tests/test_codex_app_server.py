import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import ChatCompletionRequest
from app.output_control import control_output_stream
from app.providers.codex_app_server import CodexAppServer
from app.runner import ProcessError, ProcessOutputLimitError


class FakeAppServer(CodexAppServer):
    def __init__(self, instructions_path: Path, max_output_bytes: int = 1_048_576) -> None:
        super().__init__(
            "codex", 2, instructions_path, (), max_output_bytes=max_output_bytes
        )
        self.calls: list[tuple[str, dict]] = []
        self._runtime_directory = tempfile.TemporaryDirectory(
            prefix="kessel-test-app-server-"
        )

    async def start(self) -> None:
        return None

    async def _request(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            asyncio.get_running_loop().call_soon(
                self._thread_queues["thread-1"].put_nowait,
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "thread-1", "delta": "hello"},
                },
            )
            return {"turn": {"id": "turn-1"}}
        return {}


@pytest.mark.asyncio
async def test_closing_warm_stream_interrupts_turn(tmp_path: Path) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = FakeAppServer(instructions)
    request = ChatCompletionRequest(
        model="default",
        backend="warm",
        messages=[{"role": "user", "content": "hello"}],
    )
    stream = server.stream(request, "hello", tmp_path, None)

    event = await anext(stream)
    assert event.delta == "hello"
    await stream.aclose()

    assert (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ) in server.calls
    server._runtime_directory.cleanup()


@pytest.mark.asyncio
async def test_max_tokens_interrupts_warm_turn(tmp_path: Path) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = FakeAppServer(instructions)
    request = ChatCompletionRequest(
        model="default",
        backend="warm",
        max_tokens=1,
        messages=[{"role": "user", "content": "hello"}],
    )
    stream = control_output_stream(
        server.stream(request, "hello", tmp_path, None),
        requested_model="default",
        max_tokens=1,
        stop_sequences=(),
    )

    events = [event async for event in stream]

    assert "".join(event.delta for event in events) == "hello"
    assert events[-1].result is not None
    assert events[-1].result.finish_reason == "length"
    assert (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ) in server.calls
    server._runtime_directory.cleanup()


@pytest.mark.asyncio
async def test_warm_output_limit_interrupts_turn(tmp_path: Path) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = FakeAppServer(instructions, max_output_bytes=4)
    request = ChatCompletionRequest(
        model="default",
        backend="warm",
        messages=[{"role": "user", "content": "hello"}],
    )

    with pytest.raises(ProcessOutputLimitError, match="exceeded 4 bytes"):
        async for _ in server.stream(request, "hello", tmp_path, None):
            pass

    assert (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ) in server.calls
    server._runtime_directory.cleanup()


def test_rate_limit_snapshot_uses_most_consumed_window() -> None:
    snapshot = CodexAppServer._parse_rate_limit(
        {
            "limitId": "codex",
            "primary": {"usedPercent": 25, "resetsAt": 100},
            "secondary": {"usedPercent": 80, "resetsAt": 200},
            "rateLimitReachedType": None,
        }
    )

    assert snapshot is not None
    assert snapshot.remaining_percent == 20
    assert snapshot.resets_at == 200


class RestartProbeServer(CodexAppServer):
    def __init__(self, instructions_path: Path) -> None:
        super().__init__("codex", 2, instructions_path, ())
        self.restart_count = 0
        self.restarted = asyncio.Event()

    async def _restart_once(self, process, generation: int) -> None:
        self.restart_count += 1
        self.restarted.set()


@pytest.mark.asyncio
async def test_app_server_death_fails_inflight_and_restarts_once(
    tmp_path: Path,
) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = RestartProbeServer(instructions)
    stdout = asyncio.StreamReader()
    process = SimpleNamespace(stdout=stdout)
    pending = asyncio.get_running_loop().create_future()
    queue: asyncio.Queue = asyncio.Queue()
    server._pending[1] = pending
    server._thread_queues["thread-1"] = queue
    server._generation = 1

    stdout.feed_eof()
    await server._read_stdout(process, 1)
    await server.restarted.wait()

    with pytest.raises(ProcessError, match="exited unexpectedly"):
        await pending
    assert (await queue.get())["method"] == "server/error"
    assert server.restart_count == 1


@pytest.mark.asyncio
async def test_start_failure_removes_private_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = CodexAppServer("codex", 2, instructions, ())
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source-home"))
    monkeypatch.setattr("app.providers.codex_app_server.shutil.which", lambda _: "codex")

    async def fail_spawn(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    with pytest.raises(OSError, match="spawn failed"):
        await server.start()
    assert server._runtime_directory is None


@pytest.mark.asyncio
async def test_warm_stderr_limit_fails_inflight_and_stops_process(
    tmp_path: Path, monkeypatch
) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = CodexAppServer("codex", 2, instructions, (), max_output_bytes=4)
    stderr = asyncio.StreamReader()
    process = SimpleNamespace(stderr=stderr)
    pending = asyncio.get_running_loop().create_future()
    server._pending[1] = pending
    stopped = False

    async def record_stop(candidate) -> None:
        nonlocal stopped
        assert candidate is process
        stopped = True

    monkeypatch.setattr(server, "_stop_process", record_stop)
    stderr.feed_data(b"12345")
    stderr.feed_eof()

    await server._read_stderr(process)

    with pytest.raises(ProcessOutputLimitError, match="exceeded 4 bytes"):
        await pending
    assert stopped is True
