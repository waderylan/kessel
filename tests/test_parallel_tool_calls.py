"""Parallel tool calls across the OpenAI and Anthropic routes."""

from __future__ import annotations

import json

import httpx
import pytest

from kessel_gateway.config import Settings
from kessel_gateway.main import create_app
from kessel_gateway.models import (
    AnthropicMessagesRequest,
    ChatCompletionRequest,
    FunctionCall,
    ProviderResult,
    ProviderStreamEvent,
    ToolCall,
)
from kessel_gateway.prompting import build_prompt


WEATHER_CITIES = ("Seattle", "Austin")

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]

ANTHROPIC_TOOLS = [
    {
        "name": "get_weather",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


class ParallelToolRegistry:
    """Fake provider that always answers with one weather call per city."""

    names = ("codex", "claude")

    def __init__(self) -> None:
        self.requests: list[ChatCompletionRequest] = []

    def get(self, name: str):
        return type("Provider", (), {"command": "fake"})()

    async def preflight(self, provider: str):
        return None

    async def rate_limit(self, provider: str):
        return None

    async def accepts_model(self, provider: str, model: str) -> bool:
        return True

    def _result(self, request: ChatCompletionRequest) -> ProviderResult:
        self.requests.append(request)
        return ProviderResult(
            text=None,
            model=request.model,
            tool_calls=[
                ToolCall(
                    id=f"call_{index}",
                    function=FunctionCall(
                        name="get_weather",
                        arguments=json.dumps({"city": city}),
                    ),
                )
                for index, city in enumerate(WEATHER_CITIES)
            ],
        )

    async def complete(self, provider: str, request: ChatCompletionRequest):
        return self._result(request)

    async def stream(self, provider: str, request: ChatCompletionRequest):
        yield ProviderStreamEvent(result=self._result(request))


def make_client(registry: ParallelToolRegistry) -> httpx.AsyncClient:
    settings = Settings(
        api_key=None,
        cors_origins=(),
        request_timeout_seconds=10,
        max_concurrent_requests=1,
        max_output_bytes=10_000,
        codex_command="codex",
        claude_command="claude",
        allow_unauthenticated=True,
    )
    app = create_app(settings, registry=registry)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4880"
    )


def sse_payloads(text: str) -> list[dict]:
    """Decode the JSON ``data:`` lines of a server-sent-event body."""

    payloads = []
    for frame in text.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data: ") and line != "data: [DONE]":
                payloads.append(json.loads(line[len("data: ") :]))
    return payloads


def openai_body(**extra) -> dict:
    return {
        "model": "default",
        "messages": [{"role": "user", "content": "Weather in two cities"}],
        "tools": OPENAI_TOOLS,
        "parallel_tool_calls": True,
        **extra,
    }


def anthropic_body(**extra) -> dict:
    return {
        "model": "default",
        "messages": [{"role": "user", "content": "Weather in two cities"}],
        "tools": ANTHROPIC_TOOLS,
        **extra,
    }


@pytest.mark.asyncio
async def test_openai_response_returns_every_tool_call() -> None:
    registry = ParallelToolRegistry()
    async with make_client(registry) as client:
        response = await client.post("/v1/codex/chat/completions", json=openai_body())

    assert response.status_code == 200
    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert [
        json.loads(call["function"]["arguments"])["city"]
        for call in choice["message"]["tool_calls"]
    ] == list(WEATHER_CITIES)
    assert registry.requests[0].parallel_tool_calls is True


@pytest.mark.asyncio
async def test_openai_stream_indexes_each_tool_call() -> None:
    async with make_client(ParallelToolRegistry()) as client:
        response = await client.post(
            "/v1/codex/chat/completions", json=openai_body(stream=True)
        )

    deltas = [
        choice["delta"]
        for payload in sse_payloads(response.text)
        for choice in payload.get("choices", [])
    ]
    streamed = [call for delta in deltas for call in delta.get("tool_calls", [])]
    assert [call["index"] for call in streamed] == [0, 1]
    assert [call["id"] for call in streamed] == ["call_0", "call_1"]


@pytest.mark.asyncio
async def test_anthropic_response_returns_every_tool_use_block() -> None:
    async with make_client(ParallelToolRegistry()) as client:
        response = await client.post(
            "/v1/messages",
            json=anthropic_body(tool_choice={"type": "auto", "disable_parallel_tool_use": False}),
        )

    payload = response.json()
    assert payload["stop_reason"] == "tool_use"
    assert [block["id"] for block in payload["content"]] == ["call_0", "call_1"]
    assert [block["input"]["city"] for block in payload["content"]] == list(
        WEATHER_CITIES
    )


@pytest.mark.asyncio
async def test_anthropic_stream_opens_and_closes_one_block_per_call() -> None:
    async with make_client(ParallelToolRegistry()) as client:
        response = await client.post(
            "/v1/messages",
            json=anthropic_body(
                stream=True,
                tool_choice={"type": "auto", "disable_parallel_tool_use": False},
            ),
        )

    events = [
        (payload["type"], payload.get("index"))
        for payload in sse_payloads(response.text)
        if "content_block" in payload["type"]
    ]
    assert events == [
        ("content_block_start", 0),
        ("content_block_delta", 0),
        ("content_block_stop", 0),
        ("content_block_start", 1),
        ("content_block_delta", 1),
        ("content_block_stop", 1),
    ]


@pytest.mark.parametrize(
    ("tool_choice", "expected"),
    [
        (None, False),
        ({"type": "auto"}, False),
        ({"type": "auto", "disable_parallel_tool_use": True}, False),
        ({"type": "auto", "disable_parallel_tool_use": False}, True),
    ],
)
def test_anthropic_requests_map_disable_parallel_tool_use(
    tool_choice: dict | None, expected: bool
) -> None:
    body = anthropic_body()
    if tool_choice is not None:
        body["tool_choice"] = tool_choice

    chat_request = AnthropicMessagesRequest.model_validate(body).to_chat_request()

    assert chat_request.parallel_tool_calls is expected


def test_prompt_asks_for_every_independent_call_only_when_parallel() -> None:
    def prompt_for(parallel: bool) -> str:
        return build_prompt(
            ChatCompletionRequest(
                model="default",
                messages=[{"role": "user", "content": "Weather?"}],
                tools=OPENAI_TOOLS,
                parallel_tool_calls=parallel,
            )
        )

    assert "Produce at most one function call" in prompt_for(False)
    assert "List every independent function call" in prompt_for(True)
    assert "Produce at most one function call" not in prompt_for(True)
