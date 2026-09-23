import json

import pytest

from kessel_gateway.providers.accounts import read_provider_accounts
from kessel_gateway.providers.claude import ClaudeProvider
from kessel_gateway.providers.codex import CodexProvider
from kessel_gateway.providers.health import ProviderHealth
from kessel_gateway.providers.registry import ProviderRegistry
from kessel_gateway.models import (
    ChatCompletionRequest,
    JsonSchemaDefinition,
    ProviderResult,
    ProviderStreamEvent,
    ResponseFormat,
)
from kessel_gateway.runner import (
    ProcessError,
    ProcessExitError,
    ProcessNotFoundError,
    ProcessOutputLimitError,
    ProviderInvalidModelError,
    ProcessResult,
    ProcessRunner,
    ProviderCompatibilityError,
    ProviderRateLimitError,
)


def runner() -> ProcessRunner:
    return ProcessRunner(timeout_seconds=10, max_output_bytes=10_000)


def request(
    model: str = "default",
    effort: str = "low",
    service_tier: str = "default",
) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        reasoning_effort=effort,
        service_tier=service_tier,
        messages=[{"role": "user", "content": "Hello"}],
    )


def test_codex_command_is_ephemeral_and_read_only() -> None:
    command = CodexProvider("codex", runner()).build_command(request())

    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "shell_tool" in command
    assert 'model_reasoning_effort="low"' in command
    assert 'model_reasoning_summary="none"' in command
    assert "--model" not in command
    assert command[-1] == "-"


def test_codex_parser_extracts_message_and_usage() -> None:
    output = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Hello"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 12,
                        "cached_input_tokens": 10,
                        "output_tokens": 3,
                    },
                }
            ),
        ]
    )

    result = CodexProvider("codex", runner()).parse_output(output, "default")

    assert result.text == "Hello"
    assert result.usage is not None
    assert result.usage.total_tokens == 15
    assert result.usage.prompt_tokens_details is not None
    assert result.usage.prompt_tokens_details.cached_tokens == 10


def test_codex_fast_tier_is_explicit() -> None:
    standard = CodexProvider("codex", runner()).build_command(request())
    fast = CodexProvider("codex", runner()).build_command(
        request(service_tier="fast")
    )

    assert "fast_mode" not in standard
    assert "fast_mode" in fast
    assert 'service_tier="fast"' in fast


@pytest.mark.asyncio
async def test_codex_account_info_is_normalized(monkeypatch) -> None:
    provider = CodexProvider("codex", runner())

    async def fake_account_info() -> dict:
        return {
            "account": {
                "type": "chatgpt",
                "email": "codex@example.com",
                "planType": "team",
            },
            "requiresOpenaiAuth": True,
        }

    monkeypatch.setattr(provider.app_server, "account_info", fake_account_info)

    account = await provider.account_info()

    assert account.model_dump() == {
        "provider": "codex",
        "status": "authenticated",
        "auth_method": "chatgpt",
        "account_type": "chatgpt",
        "email": "codex@example.com",
        "organization": None,
        "subscription": "team",
    }


@pytest.mark.asyncio
async def test_codex_missing_account_is_not_authenticated(monkeypatch) -> None:
    provider = CodexProvider("codex", runner())

    async def fake_account_info() -> dict:
        return {"account": None, "requiresOpenaiAuth": True}

    monkeypatch.setattr(provider.app_server, "account_info", fake_account_info)

    account = await provider.account_info()

    assert account.status == "not_authenticated"
    assert account.email is None


def test_claude_command_disables_tools_and_persistence() -> None:
    command = ClaudeProvider("claude", runner()).build_command(
        request(model="sonnet", effort="low")
    )

    assert "--no-session-persistence" in command
    assert "--safe-mode" in command
    assert "--restricted" in command
    assert command[command.index("--tools") + 1] == ""
    assert "--effort" not in command
    assert "--model" not in command
    assert ClaudeProvider("claude", runner()).environment_overrides(
        request(model="sonnet", effort="low")
    ) == {
        "ANTHROPIC_MODEL": "sonnet",
        "CLAUDE_CODE_EFFORT_LEVEL": "low",
    }
    assert command[-1] == "-"


def test_claude_parser_extracts_message_and_usage() -> None:
    output = json.dumps(
        {
            "is_error": False,
            "result": "Hello",
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 4,
                "output_tokens": 2,
            },
        }
    )

    result = ClaudeProvider("claude", runner()).parse_output(output, "default")

    assert result.text == "Hello"
    assert result.usage is not None
    assert result.usage.prompt_tokens == 14
    assert result.usage.total_tokens == 16
    assert result.usage.prompt_tokens_details is not None
    assert result.usage.prompt_tokens_details.cached_tokens == 4


def test_claude_usage_counts_cache_creation_tokens() -> None:
    # Live Claude Code reports most of a fresh prompt as cache creation.
    output = json.dumps(
        {
            "is_error": False,
            "result": "pong",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 571,
                "cache_read_input_tokens": 0,
                "output_tokens": 4,
            },
        }
    )

    result = ClaudeProvider("claude", runner()).parse_output(output, "default")

    assert result.usage is not None
    assert result.usage.prompt_tokens == 573
    assert result.usage.total_tokens == 577


class ClaudeAccountRunner:
    timeout_seconds = 10
    max_output_bytes = 10_000

    def __init__(self, payload: dict, return_code: int = 0) -> None:
        self.payload = payload
        self.return_code = return_code
        self.command: list[str] | None = None

    async def run(self, command, stdin_text, cwd, env_overrides=None):
        self.command = command
        output = json.dumps(self.payload)
        if self.return_code:
            raise ProcessExitError(self.return_code, "", output)
        return ProcessResult(stdout=output, stderr="")


@pytest.mark.asyncio
async def test_claude_account_info_is_normalized() -> None:
    account_runner = ClaudeAccountRunner(
        {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "email": "claude@example.com",
            "orgName": "Example Org",
            "orgId": "not-exposed",
            "subscriptionType": "pro",
            "configDirectory": "not-exposed",
        }
    )
    provider = ClaudeProvider("claude", account_runner)

    account = await provider.account_info()

    assert account_runner.command == ["claude", "auth", "status", "--json"]
    assert account.model_dump() == {
        "provider": "claude",
        "status": "authenticated",
        "auth_method": "claude.ai",
        "account_type": "firstParty",
        "email": "claude@example.com",
        "organization": "Example Org",
        "subscription": "pro",
    }


@pytest.mark.asyncio
async def test_claude_logged_out_status_parses_nonzero_output() -> None:
    account_runner = ClaudeAccountRunner(
        {
            "loggedIn": False,
            "authMethod": "none",
            "apiProvider": "none",
        },
        return_code=1,
    )
    provider = ClaudeProvider("claude", account_runner)

    account = await provider.account_info()

    assert account.status == "not_authenticated"
    assert account.email is None


class UnavailableAccountProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def account_info(self):
        raise self.error


@pytest.mark.asyncio
async def test_account_list_preserves_partial_provider_availability() -> None:
    registry = ProviderRegistry(
        {
            "codex": UnavailableAccountProvider(
                ProcessNotFoundError("command not found")
            ),
            "claude": UnavailableAccountProvider(
                ProcessError("account status failed")
            ),
        },
        max_concurrent_requests=1,
    )

    accounts = await registry.account_infos()

    assert [(account.provider, account.status) for account in accounts] == [
        ("codex", "not_installed"),
        ("claude", "unavailable"),
    ]


@pytest.mark.asyncio
async def test_registry_disables_only_incompatible_provider() -> None:
    registry = ProviderRegistry(
        {
            "codex": UnavailableAccountProvider(ProcessError("unused")),
            "claude": UnavailableAccountProvider(ProcessError("unused")),
        },
        max_concurrent_requests=1,
    )
    registry.disable({"codex": "missing required capabilities: --ephemeral"})

    with pytest.raises(
        ProviderCompatibilityError,
        match="missing required capabilities: --ephemeral",
    ):
        registry.get("codex")
    assert registry.get("claude") is not None
    assert registry.is_enabled("codex") is False
    assert registry.is_enabled("claude") is True


@pytest.mark.asyncio
async def test_account_reader_handles_no_installed_providers() -> None:
    accounts = await read_provider_accounts(
        [
            ProviderHealth("claude", "Claude Code", False, False),
            ProviderHealth("codex", "Codex", False, False),
        ]
    )

    assert [(account.provider, account.status) for account in accounts] == [
        ("claude", "not_installed"),
        ("codex", "not_installed"),
    ]


class ClaudeRetryRunner:
    timeout_seconds = 10

    async def stream_lines(self, command, prompt, cwd, env_overrides=None):
        yield json.dumps(
            {
                "type": "system",
                "subtype": "api_retry",
                "error": "rate_limit",
                "retry_delay_ms": 5000,
            }
        )


@pytest.mark.asyncio
async def test_claude_rate_retry_becomes_rate_limit_error() -> None:
    provider = ClaudeProvider("claude", ClaudeRetryRunner())

    with pytest.raises(ProviderRateLimitError) as error:
        await anext(provider.stream(request()))

    assert error.value.retry_after_seconds == 5


def test_codex_parser_joins_multiple_agent_messages() -> None:
    output = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item-1", "type": "agent_message", "text": "first"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item-2", "type": "agent_message", "text": "second"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )

    result = CodexProvider("codex", runner()).parse_output(output, "default")

    assert result.text == "first\n\nsecond"


class OneShotAppServer:
    """Stands in for a per-request Codex app-server in fresh streaming."""

    def __init__(self) -> None:
        self.closed = False
        self.schema = "unset"

    async def stream(self, request, prompt, cwd, schema):
        self.schema = schema
        yield ProviderStreamEvent(delta="Hel")
        yield ProviderStreamEvent(delta="lo")
        yield ProviderStreamEvent(
            result=ProviderResult(text="Hello", model=request.model)
        )

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_codex_fresh_stream_uses_one_shot_app_server(monkeypatch) -> None:
    provider = CodexProvider("codex", runner())
    servers: list[OneShotAppServer] = []

    def new_server() -> OneShotAppServer:
        servers.append(OneShotAppServer())
        return servers[-1]

    monkeypatch.setattr(provider, "_new_app_server", new_server)

    events = [event async for event in provider.stream(request())]
    second = provider.stream(request())
    assert (await anext(second)).delta == "Hel"
    await second.aclose()

    assert "".join(event.delta for event in events) == "Hello"
    assert events[-1].result is not None and events[-1].result.text == "Hello"
    # A new server per request, always closed, even when the client stops early.
    assert len(servers) == 2
    assert all(server.closed for server in servers)
    assert servers[0].schema is None
    assert provider._one_shot_servers == set()


def test_claude_reports_model_with_most_output_tokens() -> None:
    output = json.dumps(
        {
            "is_error": False,
            "result": "Hello",
            "modelUsage": {
                "claude-haiku-4-5": {"outputTokens": 5},
                "claude-sonnet-4-5": {"outputTokens": 200},
            },
        }
    )

    result = ClaudeProvider("claude", runner()).parse_output(output, "default")

    assert result.model == "claude-sonnet-4-5"


def test_claude_reports_first_model_on_tied_or_missing_usage() -> None:
    output = json.dumps(
        {
            "is_error": False,
            "result": "Hello",
            "modelUsage": {
                "claude-sonnet-4-5": {},
                "claude-haiku-4-5": {"outputTokens": 0},
            },
        }
    )

    result = ClaudeProvider("claude", runner()).parse_output(output, "default")

    assert result.model == "claude-sonnet-4-5"


def test_claude_falls_back_to_requested_model_without_usage_map() -> None:
    output = json.dumps({"is_error": False, "result": "Hello"})

    result = ClaudeProvider("claude", runner()).parse_output(output, "requested-model")

    assert result.model == "requested-model"


def test_claude_accepts_real_model_ids_and_aliases() -> None:
    provider = ClaudeProvider("claude", runner())

    assert provider.accepts_model("claude-sonnet-4-5-20250929")
    assert provider.accepts_model("claude-fable-5")
    assert provider.accepts_model("sonnet")
    assert not provider.accepts_model("gpt-4")


def test_claude_parser_raises_invalid_model_error_on_rejection() -> None:
    output = json.dumps(
        {"is_error": True, "result": "Error: model not found: claude-bogus"}
    )

    with pytest.raises(ProviderInvalidModelError):
        ClaudeProvider("claude", runner()).parse_output(output, "claude-bogus")


class ClaudeModelRejectionResultRunner:
    timeout_seconds = 10

    async def stream_lines(self, command, prompt, cwd, env_overrides=None):
        yield json.dumps(
            {"type": "result", "is_error": True, "result": "unknown model requested"}
        )


@pytest.mark.asyncio
async def test_claude_stream_raises_invalid_model_error_on_rejection() -> None:
    provider = ClaudeProvider("claude", ClaudeModelRejectionResultRunner())

    with pytest.raises(ProviderInvalidModelError):
        await anext(provider.stream(request(model="claude-bogus")))


class ClaudeLiveModelNotFoundRunner:
    """Event shape captured from Claude Code 2.1.280 for an unknown model."""

    timeout_seconds = 10

    async def stream_lines(self, command, prompt, cwd, env_overrides=None):
        message = (
            "There's an issue with the selected model (claude-bogus). It may "
            "not exist or you may not have access to it. Run --model to pick "
            "a different model."
        )
        yield json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": message}]},
                "error": "model_not_found",
            }
        )
        yield json.dumps(
            {"type": "result", "subtype": "success", "is_error": True, "result": message}
        )


@pytest.mark.asyncio
async def test_claude_stream_detects_live_model_not_found_signal() -> None:
    provider = ClaudeProvider("claude", ClaudeLiveModelNotFoundRunner())

    with pytest.raises(ProviderInvalidModelError):
        await anext(provider.stream(request(model="claude-bogus")))


def test_claude_live_model_not_found_text_is_detected() -> None:
    output = json.dumps(
        {
            "is_error": True,
            "result": (
                "There's an issue with the selected model (claude-bogus). It "
                "may not exist or you may not have access to it."
            ),
        }
    )

    with pytest.raises(ProviderInvalidModelError):
        ClaudeProvider("claude", runner()).parse_output(output, "claude-bogus")


class ClaudeModelRejectionExitRunner:
    timeout_seconds = 10
    max_output_bytes = 10_000

    async def run(self, command, stdin_text, cwd, env_overrides=None):
        raise ProcessExitError(1, "Error: invalid model specified", "")


@pytest.mark.asyncio
async def test_claude_structured_output_raises_invalid_model_error_on_rejection() -> None:
    provider = ClaudeProvider("claude", ClaudeModelRejectionExitRunner())
    schema_request = request()
    schema_request.response_format = ResponseFormat(
        type="json_schema",
        json_schema=JsonSchemaDefinition(
            name="answer", schema={"type": "object"}
        ),
    )

    with pytest.raises(ProviderInvalidModelError):
        await provider.complete(schema_request)


def test_claude_command_includes_json_schema_for_structured_requests(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "kessel_gateway.providers.claude.shutil.which", lambda command: "/usr/bin/claude"
    )
    schema_request = request()
    schema_request.response_format = ResponseFormat(
        type="json_schema",
        json_schema=JsonSchemaDefinition(
            name="answer", schema={"type": "object", "properties": {}}
        ),
    )

    command = ClaudeProvider("claude", runner()).build_command(schema_request)

    assert "--json-schema" in command
    schema_index = command.index("--json-schema") + 1
    assert json.loads(command[schema_index]) == {"type": "object", "properties": {}}
    assert command[-1] == "-"


def test_claude_command_skips_json_schema_for_cmd_shim(monkeypatch) -> None:
    monkeypatch.setattr(
        "kessel_gateway.providers.claude.shutil.which",
        lambda command: "C:/npm/claude.cmd",
    )
    schema_request = request()
    schema_request.response_format = ResponseFormat(
        type="json_schema",
        json_schema=JsonSchemaDefinition(
            name="answer", schema={"type": "object"}
        ),
    )

    command = ClaudeProvider("claude", runner()).build_command(schema_request)

    assert "--json-schema" not in command


def test_claude_command_skips_json_schema_when_oversized(monkeypatch) -> None:
    monkeypatch.setattr(
        "kessel_gateway.providers.claude.shutil.which", lambda command: "/usr/bin/claude"
    )
    huge_schema = {
        "type": "object",
        "properties": {f"field_{i}": {"type": "string"} for i in range(2000)},
    }
    schema_request = request()
    schema_request.response_format = ResponseFormat(
        type="json_schema",
        json_schema=JsonSchemaDefinition(name="answer", schema=huge_schema),
    )

    command = ClaudeProvider("claude", runner()).build_command(schema_request)

    assert "--json-schema" not in command


class ClaudeLongReplyRunner:
    timeout_seconds = 10

    def __init__(self, max_output_bytes: int, deltas: int) -> None:
        self.max_output_bytes = max_output_bytes
        self.deltas = deltas

    async def stream_lines(self, command, prompt, cwd, env_overrides=None):
        for _ in range(self.deltas):
            yield json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "0123456789"},
                    },
                }
            )
        yield json.dumps({"type": "result", "is_error": False, "result": "0123456789" * self.deltas})


@pytest.mark.asyncio
async def test_claude_reply_limit_counts_text_not_framing() -> None:
    within = ClaudeProvider("claude", ClaudeLongReplyRunner(2_000, 150))
    events = [event async for event in within.stream(request())]
    assert events[-1].result is not None
    assert len(events[-1].result.text) == 1_500

    over = ClaudeProvider("claude", ClaudeLongReplyRunner(1_000, 150))
    with pytest.raises(ProcessOutputLimitError):
        _ = [event async for event in over.stream(request())]
