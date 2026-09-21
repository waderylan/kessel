import json

import pytest
from pydantic import ValidationError

from app.models import ChatCompletionRequest, ProviderResult
from app.structured import output_schema, parse_structured_result


def tool_request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="default",
        messages=[{"role": "user", "content": "Weather in Seattle"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        tool_choice="required",
    )


def test_single_tool_schema_and_result_mapping() -> None:
    request = tool_request()
    schema = output_schema(request)
    assert schema is not None
    assert schema["properties"]["name"]["enum"] == ["get_weather"]

    result = parse_structured_result(
        request,
        ProviderResult(
            text='{"name":"get_weather","arguments":{"city":"Seattle"}}',
            model="default",
        ),
    )

    assert result.text is None
    assert result.tool_calls is not None
    assert result.tool_calls[0].function.name == "get_weather"
    assert json.loads(result.tool_calls[0].function.arguments) == {"city": "Seattle"}


def test_unsupported_tool_surfaces_are_rejected() -> None:
    payload = tool_request().model_dump()
    payload["tool_choice"] = "auto"
    with pytest.raises(ValidationError, match="tool_choice must be required"):
        ChatCompletionRequest.model_validate(payload)

    payload = tool_request().model_dump()
    payload["tools"][0]["function"]["strict"] = True
    with pytest.raises(ValidationError, match="strict tool schemas"):
        ChatCompletionRequest.model_validate(payload)
