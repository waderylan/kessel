from __future__ import annotations

import asyncio
import json
import random
import sys
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

from kessel_gateway.config import Settings
from kessel_gateway.main import create_app
from kessel_gateway.models import (
    ChatCompletionRequest,
    FunctionCall,
    ProviderResult,
    ProviderStreamEvent,
    TokenUsage,
    ToolCall,
)
from kessel_gateway.output_control import TextOutputController, control_output_stream
from kessel_gateway import output_control
from kessel_gateway.runner import ProcessRunner


def settings() -> Settings:
    return Settings(
        api_key=None,
        cors_origins=(),
        request_timeout_seconds=10,
        max_concurrent_requests=1,
        max_output_bytes=10_000,
        codex_command="codex",
        claude_command="claude",
        allow_unauthenticated=True,
    )


class FakeProvider:
    command = "fake"


class ChunkRegistry:
    names = ("codex", "claude")

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.closed = False

    def get(self, name: str) -> FakeProvider:
        return FakeProvider()

    async def preflight(self, provider_name: str):
        return None

    async def rate_limit(self, provider_name: str):
        return None

    async def accepts_model(self, provider_name: str, model: str) -> bool:
        return True

    async def complete(
        self, provider_name: str, request: ChatCompletionRequest
    ) -> ProviderResult:
        text = "".join(self.chunks)
        return ProviderResult(
            text=text,
            model=request.model,
            usage=TokenUsage(
                prompt_tokens=3,
                completion_tokens=20,
                total_tokens=23,
            ),
        )

    async def stream(self, provider_name: str, request: ChatCompletionRequest):
        text = "".join(self.chunks)
        try:
            for chunk in self.chunks:
                yield ProviderStreamEvent(delta=chunk)
                await asyncio.sleep(0)
            yield ProviderStreamEvent(
                result=ProviderResult(
                    text=text,
                    model=request.model,
                    usage=TokenUsage(
                        prompt_tokens=3,
                        completion_tokens=20,
                        total_tokens=23,
                    ),
                )
            )
        finally:
            self.closed = True


def openai_stream_payloads(response: httpx.Response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]


def anthropic_stream_payloads(response: httpx.Response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]


def test_tokenizer_encoding_is_initialized_lazily_and_cached(monkeypatch) -> None:
    calls: list[str] = []

    class FakeEncoding:
        def encode(self, text: str) -> list[int]:
            return list(range(len(text.split())))

    fake_module = SimpleNamespace(
        get_encoding=lambda name: (calls.append(name), FakeEncoding())[1]
    )
    output_control._encoding.cache_clear()
    monkeypatch.setitem(sys.modules, "tiktoken", fake_module)
    try:
        assert calls == []
        assert output_control.estimate_tokens("one two") == 2
        assert output_control.estimate_tokens("three") == 1
        assert calls == ["o200k_base"]
    finally:
        output_control._encoding.cache_clear()


_LONG_TEXT = (
    "The quick brown fox jumps over the lazy dog. " * 20
    + "Café naïve résumé \U0001f600 emoji stress test. " * 10
    + "def compute(x, y):\n    return x + y  # arithmetic\n" * 15
)


def _random_chunks(text: str, seed: int) -> list[str]:
    rng = random.Random(seed)
    chunks = []
    remaining = text
    while remaining:
        size = rng.randint(1, 37)
        chunks.append(remaining[:size])
        remaining = remaining[size:]
    return chunks


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_running_token_count_matches_full_reencode_over_random_chunkings(
    seed: int,
) -> None:
    chunks = _random_chunks(_LONG_TEXT, seed)
    controller = TextOutputController(max_tokens=10**9, stop_sequences=())

    delivered = ""
    for chunk in chunks:
        controlled = controller.push(chunk)
        delivered += controlled.emitted
        assert controlled.finish_reason is None
        assert delivered == controller.delivered
        assert output_control.estimate_tokens(delivered) == controller._token_count.count(
            delivered
        )

    assert delivered == _LONG_TEXT
    assert output_control.estimate_tokens(_LONG_TEXT) == controller._token_count.count(
        _LONG_TEXT
    )


def test_tokenizer_unavailable_raises_and_is_retried_next_call(monkeypatch) -> None:
    attempts = {"count": 0}

    class FailingModule:
        @staticmethod
        def get_encoding(name: str):
            attempts["count"] += 1
            raise OSError("no network")

    output_control._encoding.cache_clear()
    monkeypatch.setitem(sys.modules, "tiktoken", FailingModule)
    try:
        with pytest.raises(output_control.TokenizerUnavailableError) as excinfo:
            output_control.estimate_tokens("hello")
        assert excinfo.value.status_code == 503
        assert excinfo.value.error_code == "tokenizer_unavailable"
        assert "kessel setup" in excinfo.value.public_message

        # lru_cache must not remember a raised exception: retried on next call.
        with pytest.raises(output_control.TokenizerUnavailableError):
            output_control.estimate_tokens("hello")
        assert attempts["count"] == 2
    finally:
        output_control._encoding.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "backend"),
    [("codex", "fresh"), ("codex", "warm"), ("claude", "fresh")],
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_openai_max_tokens_per_backend(
    provider: str, backend: str, streaming: bool
) -> None:
    registry = ChunkRegistry(["alpha", " beta", " gamma"])
    app = create_app(settings(), registry=registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            f"/v1/{provider}/chat/completions",
            json={
                "model": "default",
                "backend": backend,
                "stream": streaming,
                "stream_options": {"include_usage": True},
                "max_tokens": 2,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 200
    assert registry.closed
    if streaming:
        payloads = openai_stream_payloads(response)
        content = "".join(
            item["choices"][0]["delta"].get("content", "")
            for item in payloads
            if item["choices"]
        )
        finish = next(
            item["choices"][0]["finish_reason"]
            for item in payloads
            if item["choices"] and item["choices"][0]["finish_reason"]
        )
        usage = next(item["usage"] for item in payloads if not item["choices"])
        assert content == "alpha beta"
        assert finish == "length"
        assert usage["completion_tokens"] == 2
    else:
        payload = response.json()
        assert payload["choices"][0]["message"]["content"] == "alpha beta"
        assert payload["choices"][0]["finish_reason"] == "length"
        assert payload["usage"]["completion_tokens"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_anthropic_max_tokens_streaming_and_buffered(streaming: bool) -> None:
    registry = ChunkRegistry(["alpha", " beta", " gamma"])
    app = create_app(settings(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "default",
                "stream": streaming,
                "max_tokens": 2,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 200
    assert registry.closed
    if streaming:
        payloads = anthropic_stream_payloads(response)
        content = "".join(
            item["delta"]["text"]
            for item in payloads
            if item["type"] == "content_block_delta"
        )
        message_delta = next(
            item for item in payloads if item["type"] == "message_delta"
        )
        assert content == "alpha beta"
        assert message_delta["delta"]["stop_reason"] == "max_tokens"
        assert message_delta["usage"]["output_tokens"] == 2
    else:
        payload = response.json()
        assert payload["content"] == [{"type": "text", "text": "alpha beta"}]
        assert payload["stop_reason"] == "max_tokens"
        assert payload["usage"]["output_tokens"] == 2


@pytest.mark.asyncio
async def test_max_completion_tokens_alias_is_enforced() -> None:
    registry = ChunkRegistry(["alpha", " beta", " gamma"])
    app = create_app(settings(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post(
            "/v1/codex/chat/completions",
            json={
                "model": "default",
                "max_completion_tokens": 2,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    payload = response.json()
    assert payload["choices"][0]["message"]["content"] == "alpha beta"
    assert payload["choices"][0]["finish_reason"] == "length"


async def run_controlled(
    chunks: list[str], stop_sequences: list[str]
) -> tuple[str, ProviderResult]:
    async def source():
        full_text = "".join(chunks)
        for chunk in chunks:
            yield ProviderStreamEvent(delta=chunk)
        yield ProviderStreamEvent(
            result=ProviderResult(
                text=full_text,
                model="default",
                usage=TokenUsage(
                    prompt_tokens=4,
                    completion_tokens=99,
                    total_tokens=103,
                ),
            )
        )

    events = [
        event
        async for event in control_output_stream(
            source(),
            requested_model="default",
            max_tokens=None,
            stop_sequences=stop_sequences,
        )
    ]
    result = events[-1].result
    assert result is not None
    return "".join(event.delta for event in events), result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        (["before STOP after"], "before "),
        (["before ST", "OP after"], "before "),
        (["before STOP"], "before "),
    ],
)
async def test_stop_sequence_positions(chunks: list[str], expected: str) -> None:
    text, result = await run_controlled(chunks, ["STOP"])

    assert text == expected
    assert "STOP" not in text
    assert result.text == expected
    assert result.finish_reason == "stop"
    assert result.stop_sequence == "STOP"


@pytest.mark.asyncio
async def test_earliest_stop_sequence_wins() -> None:
    text, result = await run_controlled(
        ["prefix SECOND middle FIRST tail"], ["FIRST", "SECOND"]
    )

    assert text == "prefix "
    assert result.stop_sequence == "SECOND"


@pytest.mark.asyncio
async def test_unmatched_stop_flushes_held_back_text() -> None:
    text, result = await run_controlled(["all cl", "ear"], ["STOP"])

    assert text == "all clear"
    assert result.text == "all clear"
    assert result.finish_reason is None
    assert result.usage is not None
    assert result.usage.completion_tokens == 99


@pytest.mark.asyncio
async def test_natural_completion_under_max_tokens_is_unchanged() -> None:
    registry = ChunkRegistry(["short"])
    app = create_app(settings(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post(
            "/v1/codex/chat/completions",
            json={
                "model": "default",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    payload = response.json()
    assert payload["choices"][0]["message"]["content"] == "short"
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"]["completion_tokens"] == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_openai_stop_sequence_streaming_and_buffered(streaming: bool) -> None:
    registry = ChunkRegistry(["before ST", "OP after"])
    app = create_app(settings(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post(
            "/v1/codex/chat/completions",
            json={
                "model": "default",
                "stream": streaming,
                "stop": ["STOP"],
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 200
    if streaming:
        payloads = openai_stream_payloads(response)
        content = "".join(
            item["choices"][0]["delta"].get("content", "")
            for item in payloads
            if item["choices"]
        )
        finish_reason = next(
            item["choices"][0]["finish_reason"]
            for item in payloads
            if item["choices"] and item["choices"][0]["finish_reason"]
        )
        assert content == "before "
        assert finish_reason == "stop"
    else:
        payload = response.json()
        assert payload["choices"][0]["message"]["content"] == "before "
        assert payload["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_anthropic_stop_sequence_field(streaming: bool) -> None:
    registry = ChunkRegistry(["before ST", "OP after"])
    app = create_app(settings(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "default",
                "stream": streaming,
                "max_tokens": 100,
                "stop_sequences": ["STOP"],
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    if streaming:
        payloads = anthropic_stream_payloads(response)
        delta = next(item for item in payloads if item["type"] == "message_delta")
        assert delta["delta"] == {
            "stop_reason": "stop_sequence",
            "stop_sequence": "STOP",
        }
    else:
        payload = response.json()
        assert payload["stop_reason"] == "stop_sequence"
        assert payload["stop_sequence"] == "STOP"
        assert payload["content"][0]["text"] == "before "


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("value", [0, -1, 1.5, "2", True])
async def test_invalid_openai_token_limits(field: str, value: object) -> None:
    app = create_app(settings(), registry=ChunkRegistry(["hello"]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post(
            "/v1/codex/chat/completions",
            json={
                "model": "default",
                field: value,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert response.json()["error"]["param"] == field


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, 1.5, "2", True])
async def test_invalid_anthropic_token_limits(value: object) -> None:
    app = create_app(settings(), registry=ChunkRegistry(["hello"]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": value,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 400
    assert response.json()["type"] == "error"
    assert response.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["json_schema", "tools"])
async def test_openai_stop_rejected_with_structured_surfaces(surface: str) -> None:
    payload: dict = {
        "model": "default",
        "stop": "STOP",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    if surface == "json_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "schema": {"type": "object"},
            },
        }
    else:
        payload["tools"] = [
            {
                "type": "function",
                "function": {"name": "answer", "parameters": {"type": "object"}},
            }
        ]
        payload["tool_choice"] = "required"

    app = create_app(settings(), registry=ChunkRegistry(["hello"]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post("/v1/codex/chat/completions", json=payload)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == "stop"
    assert "structured output or tools" in error["message"]


@pytest.mark.asyncio
async def test_anthropic_stop_rejected_with_tools() -> None:
    app = create_app(settings(), registry=ChunkRegistry(["hello"]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": 10,
                "stop_sequences": ["STOP"],
                "tools": [{"name": "answer", "input_schema": {"type": "object"}}],
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "not supported with tools" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_max_tokens_closes_and_kills_fresh_process(tmp_path: Path) -> None:
    marker = tmp_path / "completed.txt"
    script = tmp_path / "stream_then_wait.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "sys.stdin.read()\n"
        "print('alpha beta', flush=True)\n"
        "time.sleep(0.5)\n"
        "pathlib.Path(sys.argv[1]).write_text('completed')\n",
        encoding="utf-8",
    )
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=1000)

    async def source():
        process_stream = runner.stream_lines(
            [sys.executable, str(script), str(marker)], "input", tmp_path
        )
        try:
            async for line in process_stream:
                yield ProviderStreamEvent(delta=line)
        finally:
            await process_stream.aclose()

    events = [
        event
        async for event in control_output_stream(
            source(),
            requested_model="default",
            max_tokens=1,
            stop_sequences=(),
        )
    ]
    await asyncio.sleep(0.6)

    assert events[-1].result is not None
    assert events[-1].result.finish_reason == "length"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_max_tokens_does_not_bypass_tool_output() -> None:
    async def source():
        yield ProviderStreamEvent(
            result=ProviderResult(
                text=None,
                model="default",
                tool_calls=[
                    ToolCall(
                        id="call_test",
                        function=FunctionCall(
                            name="answer",
                            arguments='{"long":"tool arguments"}',
                        ),
                    )
                ],
            )
        )

    events = [
        event
        async for event in control_output_stream(
            source(),
            requested_model="default",
            max_tokens=1,
            stop_sequences=(),
        )
    ]
    result = events[-1].result

    assert result is not None
    assert result.finish_reason == "length"
    assert result.tool_calls is None
    assert result.usage is not None
    assert result.usage.completion_tokens == 0


@pytest.mark.asyncio
async def test_max_tokens_truncates_final_only_fresh_codex_text() -> None:
    async def source():
        yield ProviderStreamEvent(
            result=ProviderResult(
                text="alpha beta gamma",
                model="default",
                usage=TokenUsage(
                    prompt_tokens=5,
                    completion_tokens=3,
                    total_tokens=8,
                ),
            )
        )

    events = [
        event
        async for event in control_output_stream(
            source(),
            requested_model="default",
            max_tokens=2,
            stop_sequences=(),
        )
    ]
    result = events[-1].result

    assert "".join(event.delta for event in events) == "alpha beta"
    assert result is not None
    assert result.text == "alpha beta"
    assert result.finish_reason == "length"
    assert result.usage is not None
    assert result.usage.completion_tokens == 2
