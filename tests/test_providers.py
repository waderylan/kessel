import json

import pytest

from app.providers.accounts import read_provider_accounts
from app.providers.claude import ClaudeProvider
from app.providers.codex import CodexProvider
from app.providers.health import ProviderHealth
from app.providers.registry import ProviderRegistry
from app.models import ChatCompletionRequest
from app.runner import (
    ProcessError,
    ProcessExitError,
    ProcessNotFoundError,
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
    registry.disable({"codex"})

    with pytest.raises(ProviderCompatibilityError):
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
