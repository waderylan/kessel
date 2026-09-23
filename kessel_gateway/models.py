"""OpenAI-compatible request and response models."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]


def _validate_schema(schema: dict[str, Any]) -> dict[str, Any]:
    stack: list[tuple[object, int]] = [(schema, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > 32:
            raise ValueError("JSON schemas may not exceed 32 nested levels")
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str):
                raise ValueError("JSON Schema references are not supported")
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
    try:
        validator_for(schema).check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"invalid JSON schema: {exc.message}") from exc
    return schema


class TextPart(BaseModel):
    type: Literal["text"]
    text: str


class ChatMessage(BaseModel):
    role: Literal["developer", "system", "user", "assistant", "tool"]
    content: str | list[TextPart] | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    def text(self) -> str:
        if self.content is None:
            if self.tool_calls:
                return json.dumps(self.tool_calls, separators=(",", ":"))
            return ""
        if isinstance(self.content, str):
            return self.content
        return "\n".join(part.text for part in self.content)


class StreamOptions(BaseModel):
    include_usage: bool = False


class FunctionDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    strict: bool = False

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_schema(value)


class FunctionTool(BaseModel):
    type: Literal["function"]
    function: FunctionDefinition


class JsonSchemaDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    description: str | None = None
    schema_: dict[str, Any] = Field(alias="schema", serialization_alias="schema")
    strict: bool = False

    @field_validator("schema_")
    @classmethod
    def validate_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_schema(value)


class ResponseFormat(BaseModel):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: JsonSchemaDefinition | None = None

    @field_validator("json_schema")
    @classmethod
    def validate_json_schema(
        cls, value: JsonSchemaDefinition | None, info
    ) -> JsonSchemaDefinition | None:
        if info.data.get("type") == "json_schema" and value is None:
            raise ValueError("json_schema is required when type is json_schema")
        return value


class ChatCompletionRequest(BaseModel):
    """Small, explicit subset of the OpenAI Chat Completions request."""

    model_config = ConfigDict(extra="ignore")

    model: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "low"
    service_tier: Literal["default", "fast"] = "default"
    backend: Literal["fresh", "warm"] = "fresh"
    stream_options: StreamOptions | None = None
    tools: list[FunctionTool] = Field(default_factory=list, max_length=16)
    tool_choice: Literal["none", "auto", "required"] = "auto"
    parallel_tool_calls: bool = False
    response_format: ResponseFormat | None = None
    stream: bool = False
    stop: str | list[str] | None = None
    max_tokens: PositiveStrictInt | None = None
    max_completion_tokens: PositiveStrictInt | None = None
    n: PositiveStrictInt = 1

    @field_validator("messages")
    @classmethod
    def require_content(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        if not any(
            message.text().strip() or message.tool_call_id or message.tool_calls
            for message in messages
        ):
            raise ValueError("at least one message must contain text")
        return messages

    @field_validator("tools")
    @classmethod
    def reject_strict_tools(cls, tools: list[FunctionTool]) -> list[FunctionTool]:
        if any(tool.function.strict for tool in tools):
            raise ValueError("strict tool schemas are not supported")
        return tools

    @field_validator("stop")
    @classmethod
    def validate_stop_sequences(
        cls, value: str | list[str] | None
    ) -> str | list[str] | None:
        sequences = [value] if isinstance(value, str) else value
        if sequences is None:
            return value
        if len(sequences) > 4:
            raise ValueError("at most 4 stop sequences are supported")
        if any(not sequence for sequence in sequences):
            raise ValueError("stop sequences must not be empty")
        return value

    @model_validator(mode="after")
    def validate_tool_surface(self) -> "ChatCompletionRequest":
        active_tools = self.tools if self.tool_choice != "none" else []
        if len(active_tools) > 1:
            raise ValueError("only one function tool is supported per request")
        if active_tools and self.tool_choice != "required":
            raise ValueError(
                "tool_choice must be required when a function tool is supplied"
            )
        return self


class CompletionMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None
    refusal: None = None
    tool_calls: list["ToolCall"] | None = None


class FunctionCall(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class CompletionChoice(BaseModel):
    index: int = 0
    message: CompletionMessage
    logprobs: None = None
    finish_reason: Literal["stop", "length", "tool_calls"] = "stop"


class PromptTokensDetails(BaseModel):
    cached_tokens: int = 0


class TokenUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: PromptTokensDetails | None = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[CompletionChoice]
    usage: TokenUsage | None = None


class ProviderResult(BaseModel):
    text: str | None
    model: str
    usage: TokenUsage | None = None
    tool_calls: list[ToolCall] | None = None
    finish_reason: Literal["stop", "length"] | None = Field(
        default=None, exclude=True
    )
    stop_sequence: str | None = Field(default=None, exclude=True)


class ProviderStreamEvent(BaseModel):
    delta: str = ""
    result: ProviderResult | None = None


class ProviderAccountInfo(BaseModel):
    provider: str
    status: Literal[
        "authenticated",
        "not_authenticated",
        "not_installed",
        "unavailable",
    ]
    auth_method: str | None = None
    account_type: str | None = None
    email: str | None = None
    organization: str | None = None
    subscription: str | None = None


class ProviderAccountsResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ProviderAccountInfo]


class ModelInfo(BaseModel):
    id: str
    owned_by: str


class AnthropicMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        sections = []
        for block in self.content:
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                sections.append(block["text"])
            elif block_type == "tool_result":
                content = block.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict)
                    )
                sections.append(
                    f"Tool result for {block.get('tool_use_id', 'unknown')}: {content}"
                )
            elif block_type == "tool_use":
                sections.append(
                    "Previous tool call: "
                    + json.dumps(block, separators=(",", ":"))
                )
        return "\n".join(sections)


class AnthropicTool(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})


class AnthropicToolChoice(BaseModel):
    type: Literal["auto", "any", "tool", "none"] = "auto"
    name: str | None = None


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = "default"
    max_tokens: PositiveStrictInt | None = None
    messages: list[AnthropicMessage] = Field(min_length=1, max_length=100)
    system: str | list[dict[str, Any]] | None = None
    stream: bool = False
    tools: list[AnthropicTool] = Field(default_factory=list, max_length=1)
    tool_choice: AnthropicToolChoice | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "low"
    backend: Literal["fresh", "warm"] = "fresh"
    stop_sequences: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("stop_sequences")
    @classmethod
    def validate_stop_sequences(cls, value: list[str]) -> list[str]:
        if any(not sequence for sequence in value):
            raise ValueError("stop sequences must not be empty")
        return value

    @model_validator(mode="after")
    def validate_named_tool_choice(self) -> "AnthropicMessagesRequest":
        if self.tool_choice is None or self.tool_choice.type != "tool":
            return self
        selected_name = self.tool_choice.name
        if (
            selected_name is None
            or len(self.tools) != 1
            or self.tools[0].name != selected_name
        ):
            raise ValueError("tool_choice name must match the supplied tool")
        return self

    def to_chat_request(self) -> ChatCompletionRequest:
        messages: list[ChatMessage] = []
        if isinstance(self.system, str):
            messages.append(ChatMessage(role="system", content=self.system))
        elif isinstance(self.system, list):
            system_text = "\n".join(
                block.get("text", "")
                for block in self.system
                if block.get("type") == "text"
            )
            if system_text:
                messages.append(ChatMessage(role="system", content=system_text))
        messages.extend(
            ChatMessage(role=message.role, content=message.text())
            for message in self.messages
        )

        function_tools = [
            FunctionTool(
                type="function",
                function=FunctionDefinition(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.input_schema,
                ),
            )
            for tool in self.tools
        ]
        choice = self.tool_choice.type if self.tool_choice else "auto"
        if choice in {"any", "tool"}:
            choice = "required"
        return ChatCompletionRequest(
            model=self.model,
            messages=messages,
            stream=self.stream,
            stream_options=StreamOptions(include_usage=True),
            tools=function_tools,
            tool_choice=choice,
            reasoning_effort=self.reasoning_effort,
            backend=self.backend,
            max_tokens=self.max_tokens,
            stop=self.stop_sequences or None,
        )


class ErrorDetail(BaseModel):
    message: str
    type: str
    param: str | None = None
    code: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
