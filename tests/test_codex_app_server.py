import asyncio
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kessel_gateway.models import ChatCompletionRequest
from kessel_gateway.output_control import control_output_stream
from kessel_gateway.providers import codex_app_server as codex_app_server_module
from kessel_gateway.providers.codex_app_server import CodexAppServer
from kessel_gateway.runner import ProcessError, ProcessOutputLimitError


async def _noop_wait() -> int:
    return 0


def _exited_process(**kwargs) -> SimpleNamespace:
    """A fake asyncio subprocess that already exited (returncode=0)."""

    return SimpleNamespace(returncode=0, wait=_noop_wait, **kwargs)


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
async def test_account_info_uses_non_refreshing_protocol_request(
    tmp_path: Path,
) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = FakeAppServer(instructions)

    assert await server.account_info() == {}
    assert server.calls == [
        ("account/read", {"refreshToken": False}),
    ]
    server._runtime_directory.cleanup()


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


def test_spent_window_with_credits_is_not_exhausted() -> None:
    # Shape captured from a live edu account whose weekly window was spent
    # while requests kept succeeding on credits.
    raw = {
        "limitId": "codex",
        "primary": {"usedPercent": 0, "resetsAt": 100},
        "secondary": {"usedPercent": 100, "resetsAt": 200},
        "credits": {"hasCredits": True, "unlimited": False, "balance": None},
        "rateLimitReachedType": None,
    }
    snapshot = CodexAppServer._parse_rate_limit(raw)

    assert snapshot is not None
    assert snapshot.remaining_percent == 0
    assert snapshot.exhausted is False

    no_credits = CodexAppServer._parse_rate_limit(
        {**raw, "credits": {"hasCredits": False, "unlimited": False}}
    )
    assert no_credits is not None and no_credits.exhausted is True

    reached = CodexAppServer._parse_rate_limit(
        {**raw, "rateLimitReachedType": "rate_limit_reached"}
    )
    assert reached is not None and reached.exhausted is True


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
    process = _exited_process(stdout=stdout)
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
    monkeypatch.setattr(codex_app_server_module, "resolve_executable", lambda _: "codex")

    async def fail_spawn(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    with pytest.raises(OSError, match="spawn failed"):
        await server.start()
    assert server._runtime_directory is None


@pytest.mark.asyncio
async def test_start_never_reads_or_copies_codex_home(
    tmp_path: Path, monkeypatch
) -> None:
    """Kessel must never read, copy, or override the user's CODEX_HOME."""

    source_home = tmp_path / "source-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"secret": true}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    read_paths: list[str] = []
    original_read_text = Path.read_text

    def tracking_read_text(self, *args, **kwargs):
        read_paths.append(str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracking_read_text)

    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = CodexAppServer("codex", 2, instructions, ())
    monkeypatch.setattr(codex_app_server_module, "resolve_executable", lambda _: "codex")
    captured: dict[str, object] = {}

    async def fake_spawn(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise OSError("do not actually spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    with pytest.raises(OSError, match="do not actually spawn"):
        await server.start()

    assert not any(
        str(source_home) in path or "auth.json" in path for path in read_paths
    )
    # CODEX_HOME passes through unchanged (allowlisted), never overridden to
    # an isolated directory.
    assert captured["env"].get("CODEX_HOME") == str(source_home)


@pytest.mark.asyncio
async def test_stderr_volume_does_not_fail_inflight_or_stop_process(
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
        stopped = True

    monkeypatch.setattr(server, "_stop_process", record_stop)
    stderr.feed_data(b"1234567890")  # well beyond max_output_bytes=4
    stderr.feed_eof()

    await server._read_stderr(process)

    assert not pending.done()
    assert stopped is False
    assert bytes(server._stderr_tail) == b"1234567890"


@pytest.mark.asyncio
async def test_queue_full_fails_only_that_turn_and_keeps_reading(
    tmp_path: Path,
) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = RestartProbeServer(instructions)
    stdout = asyncio.StreamReader()
    full_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    full_queue.put_nowait({"method": "noop"})
    server._thread_queues["thread-1"] = full_queue
    server._turn_queues[("thread-1", "turn-1")] = full_queue

    overflow = json.dumps(
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "x"},
        }
    )
    other = json.dumps({"id": 7, "result": {"ok": True}})
    stdout.feed_data((overflow + "\n").encode())
    stdout.feed_data((other + "\n").encode())
    stdout.feed_eof()

    pending = asyncio.get_running_loop().create_future()
    server._pending[7] = pending
    server._generation = 1

    await server._read_stdout(_exited_process(stdout=stdout), 1)
    await server.restarted.wait()

    assert (await pending) == {"id": 7, "result": {"ok": True}}
    assert ("thread-1", "turn-1") not in server._turn_queues
    assert "thread-1" not in server._thread_queues
    drained = []
    while not full_queue.empty():
        drained.append(full_queue.get_nowait())
    assert drained[-1]["method"] == "server/error"
    # The reader kept running afterward and still restarts normally.
    assert server.restart_count == 1


@pytest.mark.asyncio
async def test_reader_ignores_non_dict_json_lines(tmp_path: Path) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = RestartProbeServer(instructions)
    stdout = asyncio.StreamReader()
    pending = asyncio.get_running_loop().create_future()
    server._pending[1] = pending
    server._generation = 1

    stdout.feed_data(b"[1, 2, 3]\n")
    stdout.feed_data((json.dumps({"id": 1, "result": {}}) + "\n").encode())
    stdout.feed_eof()

    await server._read_stdout(_exited_process(stdout=stdout), 1)

    assert (await pending) == {"id": 1, "result": {}}


@pytest.mark.asyncio
async def test_restart_budget_limits_to_three_per_window(tmp_path: Path) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = RestartProbeServer(instructions)
    server._generation = 1

    for _ in range(5):
        stdout = asyncio.StreamReader()
        stdout.feed_eof()
        await server._read_stdout(_exited_process(stdout=stdout), 1)
        await asyncio.sleep(0)  # let the scheduled restart task run

    assert server.restart_count == 3
    assert len(server._restart_times) == 3


class CompletingFakeAppServer(CodexAppServer):
    """A fake server whose single turn completes successfully."""

    def __init__(self, instructions_path: Path) -> None:
        super().__init__("codex", 2, instructions_path, ())
        self._runtime_directory = tempfile.TemporaryDirectory(
            prefix="kessel-test-app-server-"
        )

    async def start(self) -> None:
        return None

    async def _request(self, method: str, params: dict) -> dict:
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            loop = asyncio.get_running_loop()
            loop.call_soon(
                lambda: self._thread_queues["thread-1"].put_nowait(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {"threadId": "thread-1", "delta": "hi"},
                    }
                )
            )
            loop.call_soon(
                lambda: self._turn_queues[("thread-1", "turn-1")].put_nowait(
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                )
            )
            return {"turn": {"id": "turn-1"}}
        return {}


@pytest.mark.asyncio
async def test_successful_turn_resets_restart_budget(tmp_path: Path) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = CompletingFakeAppServer(instructions)
    server._restart_times = [time.monotonic()] * 3
    request = ChatCompletionRequest(
        model="default",
        backend="warm",
        messages=[{"role": "user", "content": "hi"}],
    )

    events = [
        event async for event in server.stream(request, "hi", tmp_path, None)
    ]

    assert events[-1].result is not None
    assert server._restart_times == []
    server._runtime_directory.cleanup()


@pytest.mark.asyncio
async def test_idle_timeout_stops_process(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_app_server_module, "_IDLE_SHUTDOWN_SECONDS", 0.02)
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = CodexAppServer("codex", 2, instructions, ())
    stopped: list[object] = []

    async def record_stop(process) -> None:
        stopped.append(process)

    monkeypatch.setattr(server, "_stop_process", record_stop)
    server._process = SimpleNamespace(returncode=None)

    server._begin_activity()
    server._end_activity()
    await asyncio.sleep(0.2)

    assert len(stopped) == 1
    assert server._process is None


@pytest.mark.asyncio
async def test_idle_timer_is_cancelled_by_new_activity(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(codex_app_server_module, "_IDLE_SHUTDOWN_SECONDS", 0.02)
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = CodexAppServer("codex", 2, instructions, ())
    stopped: list[object] = []

    async def record_stop(process) -> None:
        stopped.append(process)

    monkeypatch.setattr(server, "_stop_process", record_stop)
    server._process = SimpleNamespace(returncode=None)

    server._begin_activity()
    server._end_activity()
    server._begin_activity()  # a new request arrives before the idle timer fires
    await asyncio.sleep(0.1)

    assert stopped == []
    server._end_activity()
    server._cancel_idle_timer()  # avoid leaking a pending task past the test


@pytest.mark.asyncio
async def test_new_activity_never_interrupts_idle_shutdown_in_progress(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(codex_app_server_module, "_IDLE_SHUTDOWN_SECONDS", 0.01)
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = CodexAppServer("codex", 2, instructions, ())
    stopped: list[str] = []

    async def slow_stop(process) -> None:
        await asyncio.sleep(0.1)
        stopped.append("stopped")

    monkeypatch.setattr(server, "_stop_process", slow_stop)
    server._process = SimpleNamespace(returncode=None)

    server._begin_activity()
    server._end_activity()
    await asyncio.sleep(0.05)  # the timer fired; shutdown is mid-stop
    server._begin_activity()
    await server._wait_for_idle_shutdown()

    assert stopped == ["stopped"]
    assert server._process is None
    server._end_activity()
    server._cancel_idle_timer()


@pytest.mark.asyncio
async def test_multiple_agent_message_items_are_joined_with_blank_line(
    tmp_path: Path,
) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = FakeAppServer(instructions)

    async def multi_item_request(method: str, params: dict) -> dict:
        server.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            loop = asyncio.get_running_loop()

            async def emit() -> None:
                queue = server._thread_queues["thread-1"]
                queue.put_nowait(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-1",
                            "itemId": "item-1",
                            "delta": "first",
                        },
                    }
                )
                queue.put_nowait(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-1",
                            "itemId": "item-2",
                            "delta": "second",
                        },
                    }
                )
                queue.put_nowait(
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                )

            loop.create_task(emit())
            return {"turn": {"id": "turn-1"}}
        return {}

    server._request = multi_item_request
    request = ChatCompletionRequest(
        model="default",
        backend="warm",
        messages=[{"role": "user", "content": "hi"}],
    )

    events = [
        event async for event in server.stream(request, "hi", tmp_path, None)
    ]

    text = "".join(event.delta for event in events if event.delta)
    assert text == "first\n\nsecond"
    assert events[-1].result is not None
    assert events[-1].result.text == "first\n\nsecond"
    server._runtime_directory.cleanup()


@pytest.mark.asyncio
async def test_completed_item_does_not_repeat_deltas_without_item_id(
    tmp_path: Path,
) -> None:
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("test", encoding="utf-8")
    server = FakeAppServer(instructions)

    async def request_without_item_ids(method: str, params: dict) -> dict:
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            queue = server._thread_queues["thread-1"]
            for message in (
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "thread-1", "delta": "hello"},
                },
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "item": {"type": "agentMessage", "id": "item-1", "text": "hello"},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                },
            ):
                queue.put_nowait(message)
            return {"turn": {"id": "turn-1"}}
        return {}

    server._request = request_without_item_ids
    request = ChatCompletionRequest(
        model="default",
        backend="warm",
        messages=[{"role": "user", "content": "hi"}],
    )

    events = [
        event async for event in server.stream(request, "hi", tmp_path, None)
    ]

    assert "".join(event.delta for event in events if event.delta) == "hello"
    assert events[-1].result is not None
    assert events[-1].result.text == "hello"
