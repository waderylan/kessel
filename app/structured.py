"""Shared structured-output and single tool-call handling."""

from __future__ import annotations

import json
import uuid
from typing import Any

from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for

from app.models import ChatCompletionRequest, FunctionCall, ProviderResult, ToolCall
from app.runner import ProcessError


def output_schema(request: ChatCompletionRequest) -> dict[str, Any] | None:
    """Return the schema providers should enforce for this request."""

    active_tools = request.tools if request.tool_choice != "none" else []
    if active_tools:
        tool = active_tools[0]
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": [tool.function.name]},
                "arguments": tool.function.parameters,
            },
            "required": ["name", "arguments"],
            "additionalProperties": False,
        }

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
        payload = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise ProcessError("provider returned invalid structured JSON") from exc
    try:
        validator_for(schema)(schema).validate(payload)
    except ValidationError as exc:
        raise ProcessError(
            "provider returned structured output that does not match the schema"
        ) from exc

    active_tools = request.tools if request.tool_choice != "none" else []
    if not active_tools:
        result.text = json.dumps(payload, separators=(",", ":"))
        return result

    if not isinstance(payload, dict):
        raise ProcessError("provider returned an invalid tool-call envelope")
    name = payload.get("name")
    arguments = payload.get("arguments")
    allowed_names = {tool.function.name for tool in active_tools}
    if name not in allowed_names or not isinstance(arguments, dict):
        raise ProcessError("provider returned an unknown or invalid function call")
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
