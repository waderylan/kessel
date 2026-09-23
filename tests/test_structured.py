import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from kessel_gateway.models import ChatCompletionRequest, ProviderResult
from kessel_gateway.runner import ProcessError
from kessel_gateway.structured import output_schema, parse_structured_result


WEATHER_TOOL = {
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

TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "parameters": {
            "type": "object",
            "properties": {"zone": {"type": "string"}},
            "required": ["zone"],
            "additionalProperties": False,
        },
    },
}


def tool_request(tool_choice="required", tools=(WEATHER_TOOL,)) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="default",
        messages=[{"role": "user", "content": "Weather in Seattle"}],
        tools=list(tools),
        tool_choice=tool_choice,
    )


def envelope(kind: str, name=None, arguments=None, content=None) -> str:
    return json.dumps(
        {"kind": kind, "name": name, "arguments": arguments, "content": content}
    )


def test_required_tool_choice_schema_and_call() -> None:
    request = tool_request(tool_choice="required")
    schema = output_schema(request)
    assert schema is not None
    assert schema["properties"]["kind"]["enum"] == ["function_call"]
    assert schema["properties"]["name"]["enum"] == ["get_weather", None]
    assert schema["additionalProperties"] is False

    result = parse_structured_result(
        request,
        ProviderResult(
            text=envelope("function_call", "get_weather", {"city": "Seattle"}),
            model="default",
        ),
    )

    assert result.text is None
    assert result.tool_calls is not None
    assert result.tool_calls[0].function.name == "get_weather"
    assert json.loads(result.tool_calls[0].function.arguments) == {"city": "Seattle"}


def test_auto_tool_choice_allows_a_message_reply() -> None:
    request = tool_request(tool_choice="auto")
    schema = output_schema(request)
    assert schema["properties"]["kind"]["enum"] == ["function_call", "message"]

    result = parse_structured_result(
        request,
        ProviderResult(text=envelope("message", content="Hi there"), model="default"),
    )

    assert result.text == "Hi there"
    assert result.tool_calls is None


def test_auto_tool_choice_allows_a_function_call() -> None:
    request = tool_request(tool_choice="auto")

    result = parse_structured_result(
        request,
        ProviderResult(
            text=envelope("function_call", "get_weather", {"city": "Seattle"}),
            model="default",
        ),
    )

    assert result.tool_calls is not None
    assert result.tool_calls[0].function.name == "get_weather"


def test_named_tool_choice_narrows_active_tools_to_one() -> None:
    request = tool_request(
        tool_choice={"type": "function", "function": {"name": "get_time"}},
        tools=(WEATHER_TOOL, TIME_TOOL),
    )
    schema = output_schema(request)
    assert schema["properties"]["kind"]["enum"] == ["function_call"]
    assert schema["properties"]["name"]["enum"] == ["get_time", None]

    result = parse_structured_result(
        request,
        ProviderResult(
            text=envelope("function_call", "get_time", {"zone": "UTC"}),
            model="default",
        ),
    )

    assert result.tool_calls[0].function.name == "get_time"


def test_named_tool_choice_must_match_a_supplied_tool() -> None:
    with pytest.raises(ValidationError, match="must match a supplied tool"):
        tool_request(tool_choice={"type": "function", "function": {"name": "missing"}})


def test_multiple_tools_can_choose_the_second_tool() -> None:
    request = tool_request(tool_choice="required", tools=(WEATHER_TOOL, TIME_TOOL))

    result = parse_structured_result(
        request,
        ProviderResult(
            text=envelope("function_call", "get_time", {"zone": "UTC"}),
            model="default",
        ),
    )

    assert result.tool_calls[0].function.name == "get_time"
    assert json.loads(result.tool_calls[0].function.arguments) == {"zone": "UTC"}


def test_arguments_invalid_for_the_chosen_tool_are_rejected() -> None:
    request = tool_request(tool_choice="required", tools=(WEATHER_TOOL, TIME_TOOL))

    with pytest.raises(ProcessError, match="do not match the tool's schema"):
        parse_structured_result(
            request,
            ProviderResult(
                text=envelope("function_call", "get_time", {"city": "Seattle"}),
                model="default",
            ),
        )


def test_fenced_json_is_stripped_before_parsing() -> None:
    request = tool_request(tool_choice="required")
    fenced = (
        "```json\n"
        + envelope("function_call", "get_weather", {"city": "Seattle"})
        + "\n```"
    )

    result = parse_structured_result(
        request, ProviderResult(text=fenced, model="default")
    )

    assert result.tool_calls is not None
    assert result.tool_calls[0].function.name == "get_weather"


def test_plain_fence_without_language_tag_is_also_stripped() -> None:
    request = tool_request(tool_choice="required")
    fenced = "```\n" + envelope("function_call", "get_weather", {"city": "Seattle"}) + "\n```"

    result = parse_structured_result(
        request, ProviderResult(text=fenced, model="default")
    )

    assert result.tool_calls[0].function.name == "get_weather"


def test_up_to_sixteen_tools_are_allowed() -> None:
    tools = [
        {
            "type": "function",
            "function": {"name": f"tool_{index}", "parameters": {"type": "object"}},
        }
        for index in range(16)
    ]
    request = tool_request(tool_choice="auto", tools=tools)
    assert len(request.active_tools()) == 16


def test_seventeen_tools_are_rejected() -> None:
    tools = [
        {
            "type": "function",
            "function": {"name": f"tool_{index}", "parameters": {"type": "object"}},
        }
        for index in range(17)
    ]
    with pytest.raises(ValidationError):
        tool_request(tool_choice="auto", tools=tools)


def test_strict_tool_schemas_are_rejected() -> None:
    payload = tool_request().model_dump()
    payload["tools"][0]["function"]["strict"] = True
    with pytest.raises(ValidationError, match="strict tool schemas"):
        ChatCompletionRequest.model_validate(payload)


def test_malformed_or_referenced_schemas_are_rejected() -> None:
    payload = tool_request().model_dump()
    payload["tools"][0]["function"]["parameters"] = {"type": "not-a-type"}
    with pytest.raises(ValidationError, match="invalid JSON schema"):
        ChatCompletionRequest.model_validate(payload)

    payload = tool_request().model_dump()
    payload["tools"][0]["function"]["parameters"] = {
        "$ref": "https://attacker.example/schema.json"
    }
    with pytest.raises(ValidationError, match="JSON Schema references"):
        ChatCompletionRequest.model_validate(payload)

    payload["tools"][0]["function"]["parameters"] = {"$ref": "#"}
    with pytest.raises(ValidationError, match="JSON Schema references"):
        ChatCompletionRequest.model_validate(payload)


def test_structured_result_must_match_the_envelope_schema() -> None:
    with pytest.raises(ProcessError, match="does not match"):
        parse_structured_result(
            tool_request(),
            ProviderResult(text='{"kind":"function_call","name":42}', model="default"),
        )


def test_is_strict_schema_matches_openai_strict_rules() -> None:
    from kessel_gateway.structured import is_strict_schema

    strict = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"k": {"type": "string"}},
                    "required": ["k"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["name", "tags"],
        "additionalProperties": False,
    }
    assert is_strict_schema(strict) is True
    assert is_strict_schema({"type": "object"}) is False
    assert is_strict_schema({**strict, "required": ["name"]}) is False
    assert is_strict_schema({**strict, "additionalProperties": True}) is False
    nested_loose = {
        **strict,
        "properties": {**strict["properties"], "tags": {"type": "array", "items": {"type": "object"}}},
    }
    assert is_strict_schema(nested_loose) is False
    assert is_strict_schema({**strict, "oneOf": [{"type": "object"}]}) is False
    assert is_strict_schema({"type": "string"}) is False


def test_codex_receives_output_schema_only_when_strict(tmp_path) -> None:
    from kessel_gateway.providers.codex import CodexProvider

    runner = SimpleNamespace(timeout_seconds=5, max_output_bytes=1000)
    provider = CodexProvider("codex", runner)
    loose = ChatCompletionRequest(
        model="default",
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
    )
    strict = ChatCompletionRequest(
        model="default",
        messages=[{"role": "user", "content": "hi"}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "p",
                "schema": {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "required": ["a"],
                    "additionalProperties": False,
                },
            },
        },
    )

    assert "--output-schema" not in provider.build_command(loose, tmp_path)
    assert "--output-schema" in provider.build_command(strict, tmp_path)
