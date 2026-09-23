"""Shared structured-output and tool-call handling."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for

from kessel_gateway.models import (
    ChatCompletionRequest,
    FunctionCall,
    FunctionTool,
    ProviderResult,
    ToolCall,
)
from kessel_gateway.runner import ProcessError


_CODE_FENCE = re.compile(r"\A```(?:json)?\s*\n(?P<body>.*?)\n?```\s*\Z", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Strip a single surrounding markdown code fence, when the whole text is fenced."""

    match = _CODE_FENCE.match(text.strip())
    return text if match is None else match.group("body")


def _tool_envelope(tools: list[FunctionTool], required: bool) -> dict[str, Any]:
    """Object-rooted envelope so Codex's strict-schema --output-schema accepts it.

    ``kind`` distinguishes a function call from a plain message; ``name`` and
    ``arguments`` carry the call when ``kind`` is ``function_call``; ``content``
    carries the reply when ``kind`` is ``message``.
    """

    kinds = ["function_call"] if required else ["function_call", "message"]
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": kinds},
            "name": {
                "type": ["string", "null"],
                "enum": [tool.function.name for tool in tools] + [None],
            },
            "arguments": {
                "anyOf": [tool.function.parameters for tool in tools]
                + [{"type": "null"}]
            },
            "content": {"type": ["string", "null"]},
        },
        "required": ["kind", "name", "arguments", "content"],
        "additionalProperties": False,
    }


def output_schema(request: ChatCompletionRequest) -> dict[str, Any] | None:
    """Return the schema providers should enforce for this request."""

    active_tools = request.active_tools()
    if active_tools:
        return _tool_envelope(active_tools, request.requires_tool_call())

    response_format = request.response_format
    if response_format is None or response_format.type == "text":
        return None
    if response_format.type == "json_object":
        return {"type": "object"}
    if response_format.json_schema is None:
        raise ProcessError("json_schema response format is missing its schema")
    return response_format.json_schema.schema_


def parse_structured_result(
    request: ChatCompletionRequest,
    result: ProviderResult,
) -> ProviderResult:
    """Convert the provider's schema-constrained JSON into the public shape."""

    schema = output_schema(request)
    if schema is None:
        return result
    if result.text is None:
        raise ProcessError("provider returned no structured output")
    try:
        payload = json.loads(_strip_code_fence(result.text))
    except json.JSONDecodeError as exc:
        raise ProcessError("provider returned invalid structured JSON") from exc
    try:
        validator_for(schema)(schema).validate(payload)
    except ValidationError as exc:
        raise ProcessError(
            "provider returned structured output that does not match the schema"
        ) from exc

    active_tools = request.active_tools()
    if not active_tools:
        result.text = json.dumps(payload, separators=(",", ":"))
        return result

    if not isinstance(payload, dict):
        raise ProcessError("provider returned an invalid tool-call envelope")

    kind = payload.get("kind")
    if kind == "message":
        content = payload.get("content")
        if not isinstance(content, str):
            raise ProcessError("provider returned a message without text content")
        result.text = content
        result.tool_calls = None
        return result
    if kind != "function_call":
        raise ProcessError("provider returned an unknown tool-call envelope kind")

    name = payload.get("name")
    arguments = payload.get("arguments")
    tool = next(
        (tool for tool in active_tools if tool.function.name == name), None
    )
    if tool is None or not isinstance(arguments, dict):
        raise ProcessError("provider returned an unknown or invalid function call")
    try:
        validator_for(tool.function.parameters)(tool.function.parameters).validate(
            arguments
        )
    except ValidationError as exc:
        raise ProcessError(
            "provider returned arguments that do not match the tool's schema"
        ) from exc

    result.text = None
    result.tool_calls = [
        ToolCall(
            id=f"call_local_{uuid.uuid4().hex}",
            function=FunctionCall(
                name=name,
                arguments=json.dumps(arguments, separators=(",", ":")),
            ),
        )
    ]
    return result
