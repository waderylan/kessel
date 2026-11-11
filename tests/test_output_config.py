from __future__ import annotations

import json

import httpx
import pytest

from kessel_gateway.main import create_app
from kessel_gateway.models import (
    AnthropicMessagesRequest,
    ChatCompletionRequest,
    ProviderResult,
    ProviderStreamEvent,
    TokenUsage,
)
from kessel_gateway.structured import parse_structured_result
from tests.test_api import FakeRegistry, make_settings

SCHEMA = {
    "type": "object",
    "properties": {"words": {"type": "array", "items": {"type": "string"}}},
    "required": ["words"],
    "additionalProperties": False,
}


def message_body(**extra) -> dict:
    return {
        "model": "default",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "List fruits"}],
        **extra,
    }


class RawTextRegistry(FakeRegistry):
    """Returns a fixed raw provider reply, validated like a real provider."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[ChatCompletionRequest] = []

    async def stream(self, provider_name: str, request: ChatCompletionRequest):
        yield ProviderStreamEvent(result=await self.complete(provider_name, request))

    async def complete(self, provider_name: str, request: ChatCompletionRequest):
        self.requests.append(request)
        return parse_structured_result(
            request,
            ProviderResult(
                text=self.text,
                model=request.model,
                usage=TokenUsage(
                    prompt_tokens=7, completion_tokens=5, total_tokens=12
                ),
            ),
        )


async def post(registry, body: dict) -> httpx.Response:
    app = create_app(make_settings(), registry=registry)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:4880"
    ) as client:
        return await client.post("/v1/messages", json=body)


def test_output_config_maps_to_chat_request() -> None:
    request = AnthropicMessagesRequest.model_validate(
        message_body(
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": SCHEMA},
            }
        )
    ).to_chat_request()

    assert request.reasoning_effort == "low"
    assert request.response_format.type == "json_schema"
    assert request.response_format.json_schema.schema_ == SCHEMA


def test_effort_max_clamps_to_xhigh() -> None:
    request = AnthropicMessagesRequest.model_validate(
        message_body(output_config={"effort": "max"})
    ).to_chat_request()

    assert request.reasoning_effort == "xhigh"
    assert request.response_format is None


def test_unknown_fields_are_ignored() -> None:
    request = AnthropicMessagesRequest.model_validate(
        message_body(
            unknown_top_level=1,
            output_config={
                "something_new": True,
                "format": {"type": "json_schema", "schema": SCHEMA, "extra": 1},
            },
        )
    ).to_chat_request()

    assert request.response_format is not None


@pytest.mark.asyncio
async def test_json_output_is_single_text_block() -> None:
    registry = RawTextRegistry('```json\n{"words": ["apple", "banana"]}\n```')
    response = await post(
        registry,
        message_body(
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": SCHEMA},
            }
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["content"]) == 1
    block = payload["content"][0]
    assert block["type"] == "text"
    assert json.loads(block["text"]) == {"words": ["apple", "banana"]}
    assert block["text"].startswith("{")
    assert payload["stop_reason"] == "end_turn"
    assert payload["usage"] == {"input_tokens": 7, "output_tokens": 5}
    assert registry.requests[0].response_format is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["Apple, banana, and orange.", '{"words": "not-a-list"}']
)
async def test_non_conforming_output_returns_error(text: str) -> None:
    response = await post(
        RawTextRegistry(text),
        message_body(
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}}
        ),
    )

    assert response.status_code >= 400
    assert response.json()["type"] == "error"


@pytest.mark.asyncio
async def test_stop_sequences_rejected_with_structured_output() -> None:
    response = await post(
        RawTextRegistry("{}"),
        message_body(
            stop_sequences=["x"],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        ),
    )

    assert response.status_code == 400
    assert "structured output" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_invalid_effort_returns_400() -> None:
    response = await post(
        FakeRegistry(), message_body(output_config={"effort": "turbo"})
    )

    assert response.status_code == 400
