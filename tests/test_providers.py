import json

import pytest

from app.providers.claude import ClaudeProvider
from app.providers.codex import CodexProvider
from app.models import ChatCompletionRequest
from app.runner import ProcessRunner, ProviderRateLimitError


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
