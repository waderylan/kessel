"""Claude Code CLI adapter."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path

from kessel_gateway.models import (
    ChatCompletionRequest,
    ProviderAccountInfo,
    ProviderResult,
    ProviderStreamEvent,
    TokenUsage,
)
from kessel_gateway.prompting import build_prompt
from kessel_gateway.providers.base import ProviderAdapter, uses_default_model
from kessel_gateway.rate_limits import RateLimitSnapshot
from kessel_gateway.runner import (
    ProcessError,
    ProcessExitError,
    ProviderInvalidModelError,
    ProviderRateLimitError,
    check_reply_size,
    provider_error_from_message,
)
from kessel_gateway.structured import output_schema

# Real Claude model ids, e.g. "claude-sonnet-4-5-20250929" or "claude-fable-5".
_MODEL_ID_PATTERN = re.compile(r"^claude-[A-Za-z0-9._-]+$")

# Conservative, case-insensitive detection of Claude rejecting the requested
# model, from either a result's error text or stderr. Requires "model" to be
# present alongside one of these qualifiers to avoid false positives.
_MODEL_REJECTION_QUALIFIERS = (
    "not found",
    "invalid model",
    "does not exist",
    "may not exist",
    "unknown model",
    "issue with the selected model",
)


def _is_model_rejection(message: str) -> bool:
    normalized = message.lower()
    if "model" not in normalized:
        return False
    return any(qualifier in normalized for qualifier in _MODEL_REJECTION_QUALIFIERS)


def _claude_error_from_message(
    message: str,
    retry_after_seconds: int | None = None,
    *,
    provider: str,
    requested_model: str,
) -> ProcessError:
    if _is_model_rejection(message):
        return ProviderInvalidModelError(
            f"Claude rejected model {requested_model!r}: {message}"
        )
    return provider_error_from_message(message, retry_after_seconds, provider=provider)


def _claude_error_from_exit(
    exc: ProcessExitError, requested_model: str
) -> ProcessError:
    if _is_model_rejection(exc.stderr):
        return ProviderInvalidModelError(
            f"Claude rejected model {requested_model!r}: {exc.stderr}"
        )
    return exc


class ClaudeProvider(ProviderAdapter):
    name = "claude"
    MODEL_ALIASES = {"sonnet", "opus", "haiku", "fable", "mythos"}

    def accepts_model(self, model: str) -> bool:
        return (
            super().accepts_model(model)
            or model in self.MODEL_ALIASES
            or bool(_MODEL_ID_PATTERN.match(model))
        )

    def environment_overrides(
        self, request: ChatCompletionRequest
    ) -> dict[str, str]:
        overrides = {"CLAUDE_CODE_EFFORT_LEVEL": request.reasoning_effort}
        if not uses_default_model(self.name, request.model):
            overrides["ANTHROPIC_MODEL"] = request.model
        return overrides

    async def complete(self, request: ChatCompletionRequest) -> ProviderResult:
        if output_schema(request) is not None:
            try:
                return await super().complete(request)
            except ProcessExitError as exc:
                raise _claude_error_from_exit(exc, request.model) from exc
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

    async def account_info(self) -> ProviderAccountInfo:
        temporary = await asyncio.to_thread(
            tempfile.TemporaryDirectory, prefix="kessel-claude-account-"
        )
        try:
            try:
                result = await self.runner.run(
                    [self.command, "auth", "status", "--json"],
                    "",
                    Path(temporary.name),
                )
                output = result.stdout
            except ProcessExitError as exc:
                output = exc.stdout
                if not output.strip():
                    raise
            return self._parse_account_info(output)
        finally:
            await asyncio.to_thread(temporary.cleanup)

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
            "--tools",
            "",
            "--system-prompt",
            (
                "You are a stateless text assistant. Answer the supplied "
                "conversation directly and concisely. Do not use tools."
            ),
        ]
        schema = output_schema(request)
        if schema is not None:
            serialized = json.dumps(schema, separators=(",", ":"))
            # cmd.exe/.bat shims re-tokenize arguments, which is not safe for
            # an arbitrary JSON schema; fall back to the prompt there. Also
            # skip very large schemas rather than risk truncation.
            executable = shutil.which(self.command) or self.command
            if (
                Path(executable).suffix.lower() not in {".cmd", ".bat"}
                and len(serialized.encode("utf-8")) <= 16_384
            ):
                command.extend(["--json-schema", serialized])
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
        reply_bytes = 0
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
            line_stream = self.runner.stream_lines(
                command,
                prompt,
                cwd,
                self.environment_overrides(request),
            )
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
                                reply_bytes += len(text.encode("utf-8"))
                                check_reply_size(
                                    reply_bytes,
                                    getattr(self.runner, "max_output_bytes", None),
                                )
                                yield ProviderStreamEvent(delta=text)
                    elif (
                        event.get("type") == "system"
                        and event.get("subtype") == "init"
                        and isinstance(event.get("model"), str)
                        and event["model"]
                    ):
                        yield ProviderStreamEvent(model=event["model"])
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
                    elif (
                        event.get("type") == "assistant"
                        and event.get("error") == "model_not_found"
                    ):
                        raise ProviderInvalidModelError(
                            f"Claude rejected model {request.model!r}"
                        )
                    elif event.get("type") == "result":
                        if event.get("is_error"):
                            snapshot = await self.rate_limit()
                            raise _claude_error_from_message(
                                event.get("result") or "Claude reported an error",
                                snapshot.retry_after_seconds if snapshot else None,
                                provider=self.name,
                                requested_model=request.model,
                            )
                        final_result = ProviderResult(
                            text=event.get("result") or full_text,
                            model=self._reported_model(
                                event.get("modelUsage"), request.model
                            ),
                            usage=self._parse_usage(event.get("usage")),
                        )
                        self.set_rate_limit(None)
        except ProcessExitError as exc:
            raise _claude_error_from_exit(exc, request.model) from exc
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
            raise _claude_error_from_message(
                payload.get("result") or "Claude reported an error",
                provider=self.name,
                requested_model=requested_model,
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
        model = self._reported_model(payload.get("modelUsage"), requested_model)
        return ProviderResult(text=text, model=model, usage=usage)

    @staticmethod
    def _reported_model(model_usage: object, requested_model: str) -> str:
        """Pick the model with the most output tokens from a modelUsage map.

        Falls back to the first key (preserved by stable, insertion-ordered
        iteration on ties), then to the requested model.
        """

        if not isinstance(model_usage, dict) or not model_usage:
            return requested_model
        best_key: str | None = None
        best_tokens = -1
        for key, value in model_usage.items():
            tokens = 0
            if isinstance(value, dict):
                raw = value.get("outputTokens", 0)
                if isinstance(raw, (int, float)):
                    tokens = raw
            if best_key is None or tokens > best_tokens:
                best_key = key
                best_tokens = tokens
        return str(best_key) if best_key is not None else requested_model

    @classmethod
    def _parse_account_info(cls, output: str) -> ProviderAccountInfo:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ProcessError("Claude returned invalid account JSON") from exc
        if not isinstance(payload, dict):
            raise ProcessError("Claude returned invalid account JSON")

        authenticated = payload.get("loggedIn") is True
        return ProviderAccountInfo(
            provider=cls.name,
            status=("authenticated" if authenticated else "not_authenticated"),
            auth_method=cls._optional_string(payload.get("authMethod")),
            account_type=cls._optional_string(payload.get("apiProvider")),
            email=cls._optional_string(payload.get("email")),
            organization=cls._optional_string(payload.get("orgName")),
            subscription=cls._optional_string(payload.get("subscriptionType")),
        )

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _parse_usage(raw_usage: object) -> TokenUsage | None:
        if not isinstance(raw_usage, dict):
            return None
        # Claude splits the prompt into uncached, cache-written, and
        # cache-read tokens; OpenAI's prompt_tokens counts all three.
        cached_tokens = int(raw_usage.get("cache_read_input_tokens") or 0)
        prompt_tokens = (
            int(raw_usage.get("input_tokens") or 0)
            + int(raw_usage.get("cache_creation_input_tokens") or 0)
            + cached_tokens
        )
        completion_tokens = int(raw_usage.get("output_tokens", 0))
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            prompt_tokens_details={"cached_tokens": cached_tokens},
        )
