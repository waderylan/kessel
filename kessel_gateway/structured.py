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


def _call_schema(tool: FunctionTool) -> dict[str, Any]:
    """Schema for one call to ``tool``: its name paired with its own arguments."""

    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": [tool.function.name]},
            "arguments": tool.function.parameters,
        },
        "required": ["name", "arguments"],
        "additionalProperties": False,
    }


def _tool_envelope(
    tools: list[FunctionTool], required: bool, max_calls: int
) -> dict[str, Any]:
    """Object-rooted envelope so Codex's strict-schema --output-schema accepts it.

    ``kind`` distinguishes a function call from a plain message. ``calls`` lists
    the function calls (at most ``max_calls``) when ``kind`` is ``function_call``
    and is empty otherwise. ``content`` carries the reply when ``kind`` is
    ``message``.
    """

    kinds = ["function_call"] if required else ["function_call", "message"]
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": kinds},
            "calls": {
                "type": "array",
                "items": {"anyOf": [_call_schema(tool) for tool in tools]},
                "maxItems": max_calls,
            },
            "content": {"type": ["string", "null"]},
        },
        "required": ["kind", "calls", "content"],
        "additionalProperties": False,
    }


def output_schema(request: ChatCompletionRequest) -> dict[str, Any] | None:
    """Return the schema providers should enforce for this request."""

    active_tools = request.active_tools()
    if active_tools:
        return _tool_envelope(
            active_tools, request.requires_tool_call(), request.max_tool_calls()
        )

    response_format = request.response_format
    if response_format is None or response_format.type == "text":
        return None
    if response_format.type == "json_object":
        return {"type": "object"}
    if response_format.json_schema is None:
        raise ProcessError("json_schema response format is missing its schema")
    return response_format.json_schema.schema_


# Keywords OpenAI strict structured outputs reject.
_NON_STRICT_KEYWORDS = frozenset(
    {
        "allOf",
        "dependentRequired",
        "dependentSchemas",
        "else",
        "if",
        "not",
        "oneOf",
        "patternProperties",
        "then",
        "unevaluatedProperties",
    }
)


def is_strict_schema(schema: object) -> bool:
    """Return whether OpenAI strict structured outputs accept ``schema``.

    Codex's ``--output-schema`` and app-server ``outputSchema`` reject any
    other schema outright, e.g. a bare ``{"type": "object"}``, an optional
    property, or an object without ``additionalProperties: false``.
    """

    return (
        isinstance(schema, dict)
        and schema.get("type") == "object"
        and _is_strict_node(schema)
    )


def _is_strict_node(node: object) -> bool:
    if isinstance(node, list):
        return all(_is_strict_node(item) for item in node)
    if not isinstance(node, dict):
        return True
    if _NON_STRICT_KEYWORDS & node.keys():
        return False
    node_type = node.get("type")
    if (
        node_type == "object"
        or (isinstance(node_type, list) and "object" in node_type)
        or "properties" in node
    ):
        properties = node.get("properties", {})
        if not isinstance(properties, dict):
            return False
        if node.get("additionalProperties") is not False:
            return False
        if set(node.get("required", [])) != set(properties):
            return False
    for key in ("properties", "$defs", "definitions"):
        children = node.get(key)
        if isinstance(children, dict) and not all(
            _is_strict_node(child) for child in children.values()
        ):
            return False
    for key in ("items", "prefixItems", "anyOf"):
        if key in node and not _is_strict_node(node[key]):
            return False
    return True


def strict_output_schema(request: ChatCompletionRequest) -> dict[str, Any] | None:
    """Return the output schema only when strict providers can enforce it.

    Otherwise the schema still reaches the model through the prompt and
    ``parse_structured_result`` still validates the reply against it.
    """

    schema = output_schema(request)
    return schema if is_strict_schema(schema) else None


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

    result.text = None
    result.tool_calls = _parse_tool_calls(payload.get("calls"), active_tools, request)
    return result


def _parse_tool_calls(
    raw_calls: object,
    active_tools: list[FunctionTool],
    request: ChatCompletionRequest,
) -> list[ToolCall]:
    """Turn the envelope's ``calls`` list into validated public tool calls."""

    if not isinstance(raw_calls, list) or not raw_calls:
        raise ProcessError("provider returned a function call without any calls")
    if len(raw_calls) > request.max_tool_calls():
        raise ProcessError("provider returned more function calls than allowed")
    return [_parse_tool_call(raw_call, active_tools) for raw_call in raw_calls]


def _parse_tool_call(raw_call: object, active_tools: list[FunctionTool]) -> ToolCall:
    """Validate one ``{"name", "arguments"}`` entry against the tool it names."""

    if not isinstance(raw_call, dict):
        raise ProcessError("provider returned an unknown or invalid function call")
    name = raw_call.get("name")
    arguments = raw_call.get("arguments")
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
    return ToolCall(
        id=f"call_local_{uuid.uuid4().hex}",
        function=FunctionCall(
            name=name,
            arguments=json.dumps(arguments, separators=(",", ":")),
        ),
    )
