"""Claude Code CLI adapter."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path

from app.models import (
    ChatCompletionRequest,
    ProviderResult,
    ProviderStreamEvent,
    TokenUsage,
)
from app.prompting import build_prompt
from app.providers.base import ProviderAdapter, uses_default_model
from app.rate_limits import RateLimitSnapshot
from app.runner import (
    ProcessError,
    ProviderRateLimitError,
    provider_error_from_message,
)
from app.structured import output_schema


class ClaudeProvider(ProviderAdapter):
    name = "claude"

    async def complete(self, request: ChatCompletionRequest) -> ProviderResult:
        if output_schema(request) is not None:
            return await super().complete(request)
        final_result: ProviderResult | None = None
        provider_stream = self.stream(request)
        async with aclosing(provider_stream):
            async for event in provider_stream:
                if event.result is not None:
                    final_result = event.result
        if final_result is None:
            raise ProcessError("Claude completed without an assistant message")
        return final_result

    async def rate_limit(self) -> RateLimitSnapshot | None:
        snapshot = self._rate_limit
        if (
            snapshot is not None
            and snapshot.resets_at is not None
            and snapshot.resets_at <= int(time.time())
        ):
            self.set_rate_limit(None)
            return None
        return snapshot

    def build_command(
        self, request: ChatCompletionRequest, cwd: Path | None = None
    ) -> list[str]:
        command = [
            self.command,
            "--print",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--permission-prompts",
            "none",
            "--safe-mode",
            "--restricted",
            "--effort",
            request.reasoning_effort,
            "--tools",
            "",
            "--system-prompt",
            (
                "You are a stateless text assistant. Answer the supplied "
                "conversation directly and concisely. Do not use tools."
            ),
        ]
        if not uses_default_model(self.name, request.model):
            command.extend(["--model", request.model])
        schema = output_schema(request)
        if schema is not None:
            command.extend(
                ["--json-schema", json.dumps(schema, separators=(",", ":"))]
            )
        command.append("-")
        return command

    async def stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        if output_schema(request) is not None:
            yield ProviderStreamEvent(result=await self.complete(request))
            return

        prompt = build_prompt(request)
        full_text = ""
        final_result: ProviderResult | None = None
        temporary = await asyncio.to_thread(
            tempfile.TemporaryDirectory, prefix="kessel-claude-"
        )
        try:
            cwd = Path(temporary.name)
            command = self.build_command(request, cwd)
            output_index = command.index("json")
            command[output_index] = "stream-json"
            command[output_index + 1 : output_index + 1] = [
                "--verbose",
                "--include-partial-messages",
            ]
            line_stream = self.runner.stream_lines(command, prompt, cwd)
            async with aclosing(line_stream):
                async for line in line_stream:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ProcessError(
                            "Claude returned invalid JSONL output"
                        ) from exc

                    if event.get("type") == "stream_event":
                        inner = event.get("event", {})
                        delta = inner.get("delta", {})
                        if (
                            inner.get("type") == "content_block_delta"
                            and delta.get("type") == "text_delta"
                        ):
                            text = delta.get("text", "")
                            if isinstance(text, str) and text:
                                full_text += text
                                yield ProviderStreamEvent(delta=text)
                    elif (
                        event.get("type") == "system"
                        and event.get("subtype") == "api_retry"
                        and event.get("error") == "rate_limit"
                    ):
                        retry_delay_ms = event.get("retry_delay_ms")
                        retry_after = (
                            max(1, int(retry_delay_ms / 1000))
                            if isinstance(retry_delay_ms, (int, float))
                            else None
                        )
                        self.set_rate_limit(
                            RateLimitSnapshot(
                                remaining_percent=0,
                                resets_at=(
                                    int(time.time()) + retry_after
                                    if retry_after is not None
                                    else None
                                ),
                                limit_id="claude-retry",
                                retry_after_seconds=retry_after,
                            )
                        )
                        raise ProviderRateLimitError(
                            "Claude rate limit reached",
                            retry_after_seconds=retry_after,
                        )
                    elif event.get("type") == "result":
                        if event.get("is_error"):
                            snapshot = await self.rate_limit()
                            raise provider_error_from_message(
                                event.get("result") or "Claude reported an error",
                                snapshot.retry_after_seconds if snapshot else None,
                            )
                        model = event.get("modelUsage") or request.model
                        if isinstance(model, dict):
                            model = next(iter(model), request.model)
                        final_result = ProviderResult(
                            text=event.get("result") or full_text,
                            model=str(model),
                            usage=self._parse_usage(event.get("usage")),
                        )
                        self.set_rate_limit(None)
        finally:
            await asyncio.to_thread(temporary.cleanup)

        if final_result is None or not final_result.text:
            raise ProcessError("Claude completed without an assistant message")
        yield ProviderStreamEvent(result=final_result)

    def parse_output(self, output: str, requested_model: str) -> ProviderResult:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ProcessError("Claude returned invalid JSON output") from exc

        if payload.get("is_error"):
            raise provider_error_from_message(
                payload.get("result") or "Claude reported an error"
            )
        structured = payload.get("structured_output")
        text = (
            json.dumps(structured, separators=(",", ":"))
            if structured is not None
            else payload.get("result")
        )
        if not isinstance(text, str) or not text.strip():
            raise ProcessError("Claude completed without an assistant message")

        usage = self._parse_usage(payload.get("usage"))
        model = payload.get("modelUsage") or requested_model
        if isinstance(model, dict):
            model = next(iter(model), requested_model)
        return ProviderResult(text=text, model=str(model), usage=usage)

    @staticmethod
    def _parse_usage(raw_usage: object) -> TokenUsage | None:
        if not isinstance(raw_usage, dict):
            return None
        prompt_tokens = int(raw_usage.get("input_tokens", 0)) + int(
            raw_usage.get("cache_read_input_tokens", 0)
        )
        cached_tokens = int(raw_usage.get("cache_read_input_tokens", 0))
        completion_tokens = int(raw_usage.get("output_tokens", 0))
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            prompt_tokens_details={"cached_tokens": cached_tokens},
        )
