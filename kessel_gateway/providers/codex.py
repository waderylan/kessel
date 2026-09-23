"""Codex CLI adapter."""

from __future__ import annotations

import asyncio
import json
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
from kessel_gateway.providers.codex_app_server import CodexAppServer
from kessel_gateway.providers.base import (
    ProviderAdapter,
    uses_default_model,
    write_output_schema,
)
from kessel_gateway.rate_limits import RateLimitSnapshot
from kessel_gateway.runner import (
    ProcessError,
    provider_error_from_message,
)
from kessel_gateway.structured import output_schema, strict_output_schema


class CodexProvider(ProviderAdapter):
    name = "codex"

    DISABLED_FEATURES = (
        "apps",
        "browser_use",
        "computer_use",
        "goals",
        "hooks",
        "image_generation",
        "multi_agent",
        "plugins",
        "remote_plugin",
        "shell_tool",
        "skill_mcp_dependency_install",
        "skill_search",
        "sleep_tool",
        "tool_suggest",
        "unified_exec",
        "view_image",
    )

    def __init__(self, command: str, runner) -> None:
        super().__init__(command, runner)
        self.app_server = self._new_app_server()
        self._one_shot_servers: set[CodexAppServer] = set()

    def _new_app_server(self) -> CodexAppServer:
        return CodexAppServer(
            command=self.command,
            timeout_seconds=self.runner.timeout_seconds,
            max_output_bytes=self.runner.max_output_bytes,
            instructions_path=Path(__file__).with_name("codex_instructions.txt").resolve(),
            disabled_features=self.DISABLED_FEATURES,
        )

    async def close(self) -> None:
        await asyncio.gather(
            self.app_server.close(),
            *(server.close() for server in tuple(self._one_shot_servers)),
            return_exceptions=True,
        )

    async def list_models(self) -> list[str]:
        return await self.app_server.list_models()

    async def rate_limit(self) -> RateLimitSnapshot | None:
        snapshot = await self.app_server.rate_limit()
        self.set_rate_limit(snapshot)
        return snapshot

    async def account_info(self) -> ProviderAccountInfo:
        response = await self.app_server.account_info()
        account = response.get("account")
        if not isinstance(account, dict):
            return ProviderAccountInfo(
                provider=self.name,
                status="not_authenticated",
            )

        account_type = self._optional_string(account.get("type"))
        auth_method = {
            "apiKey": "api_key",
            "chatgpt": "chatgpt",
        }.get(account_type or "", account_type)
        return ProviderAccountInfo(
            provider=self.name,
            status="authenticated",
            auth_method=auth_method,
            account_type=account_type,
            email=self._optional_string(account.get("email")),
            subscription=self._optional_string(account.get("planType")),
        )

    async def complete(self, request: ChatCompletionRequest) -> ProviderResult:
        if request.backend == "fresh":
            return await super().complete(request)
        prompt = build_prompt(request)
        final: ProviderResult | None = None
        app_stream = self.app_server.stream(
            request, prompt, Path("."), strict_output_schema(request)
        )
        async with aclosing(app_stream):
            async for event in app_stream:
                if event.result is not None:
                    final = event.result
        if final is None:
            raise ProcessError("Codex App Server returned no result")
        from kessel_gateway.structured import parse_structured_result

        return parse_structured_result(request, final)

    def build_command(
        self, request: ChatCompletionRequest, cwd: Path | None = None
    ) -> list[str]:
        instructions_path = (
            Path(__file__).with_name("codex_instructions.txt").resolve().as_posix()
        )
        command = [
            self.command,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--config",
            f"model_instructions_file={json.dumps(instructions_path)}",
            "--config",
            "skills.max_context_tokens=1",
            "--config",
            "agents.enabled=false",
            "--config",
            f'model_reasoning_effort="{request.reasoning_effort}"',
            "--config",
            'model_reasoning_summary="none"',
            "--config",
            'model_verbosity="low"',
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
        ]
        for feature in self.DISABLED_FEATURES:
            command.extend(["--disable", feature])
        if request.service_tier == "fast":
            command.extend(
                ["--enable", "fast_mode", "--config", 'service_tier="fast"']
            )
        # Codex only accepts strict schemas; any other schema is enforced by
        # the prompt and Kessel's own validation instead.
        strict_schema = strict_output_schema(request)
        if cwd is not None and strict_schema is not None:
            schema_path = write_output_schema(request, cwd, strict_schema)
            if schema_path is not None:
                command.extend(["--output-schema", str(schema_path)])
        if not uses_default_model(self.name, request.model):
            command.extend(["--model", request.model])
        command.append("-")
        return command

    async def stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        if request.backend == "warm":
            prompt = build_prompt(request)
            schema = output_schema(request)
            buffered_result: ProviderResult | None = None
            app_stream = self.app_server.stream(
                request, prompt, Path("."), strict_output_schema(request)
            )
            async with aclosing(app_stream):
                async for event in app_stream:
                    if schema is None:
                        yield event
                    elif event.result is not None:
                        buffered_result = event.result
            if schema is not None:
                if buffered_result is None:
                    raise ProcessError("Codex App Server returned no result")
                from kessel_gateway.structured import parse_structured_result

                yield ProviderStreamEvent(
                    result=parse_structured_result(request, buffered_result)
                )
            return
        if output_schema(request) is not None:
            yield ProviderStreamEvent(result=await self.complete(request))
            return

        # `codex exec --json` only reports a message once it is finished, so
        # stream fresh requests through a one-shot app-server instead: still a
        # new process and a new ephemeral thread per request, but with real
        # deltas and an interrupt as soon as the client stops reading.
        server = self._new_app_server()
        self._one_shot_servers.add(server)
        try:
            app_stream = server.stream(
                request, build_prompt(request), Path("."), None
            )
            async with aclosing(app_stream):
                async for event in app_stream:
                    yield event
        finally:
            self._one_shot_servers.discard(server)
            await server.close()

    def parse_output(self, output: str, requested_model: str) -> ProviderResult:
        # Concatenate every completed agent_message item in order, matching
        # the streaming path's separator so both backends behave the same.
        message_parts: list[str] = []
        usage: TokenUsage | None = None

        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProcessError("Codex returned invalid JSONL output") from exc

            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        message_parts.append(text)
            elif event.get("type") == "turn.completed":
                usage = self._parse_usage(event.get("usage", {}))
            elif event.get("type") in {"turn.failed", "error"}:
                message = event.get("message") or event.get("error", {}).get(
                    "message"
                )
                raise provider_error_from_message(
                    message or "Codex reported an error",
                    provider=self.name,
                )

        if not message_parts:
            raise ProcessError("Codex completed without an assistant message")
        return ProviderResult(
            text="\n\n".join(message_parts),
            model=requested_model,
            usage=usage,
        )

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _parse_usage(raw_usage: object) -> TokenUsage | None:
        if not isinstance(raw_usage, dict):
            return None
        prompt_tokens = int(raw_usage.get("input_tokens", 0))
        cached_tokens = int(raw_usage.get("cached_input_tokens", 0))
        completion_tokens = int(raw_usage.get("output_tokens", 0))
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            prompt_tokens_details={"cached_tokens": cached_tokens},
        )
