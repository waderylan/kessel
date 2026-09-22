"""Shared provider adapter contract."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path

from app.models import (
    ChatCompletionRequest,
    ProviderAccountInfo,
    ProviderResult,
    ProviderStreamEvent,
)
from app.prompting import build_prompt
from app.rate_limits import RateLimitSnapshot
from app.runner import ProcessRunner, ProviderRateLimitError
from app.structured import output_schema, parse_structured_result


class ProviderAdapter(ABC):
    name: str

    def __init__(self, command: str, runner: ProcessRunner) -> None:
        self.command = command
        self.runner = runner
        self._observed_models: set[str] = set()
        self._rate_limit: RateLimitSnapshot | None = None

    async def close(self) -> None:
        """Release optional long-lived provider resources."""

    async def complete(self, request: ChatCompletionRequest) -> ProviderResult:
        prompt = build_prompt(request)
        temporary = await asyncio.to_thread(
            tempfile.TemporaryDirectory, prefix=f"kessel-{self.name}-"
        )
        try:
            cwd = Path(temporary.name)
            command = await asyncio.to_thread(
                self.build_command, request, cwd
            )
            result = await self.runner.run(
                command,
                prompt,
                cwd,
                self.environment_overrides(request),
            )
        finally:
            await asyncio.to_thread(temporary.cleanup)
        parsed = self.parse_output(result.stdout, request.model)
        parsed = parse_structured_result(request, parsed)
        self.observe_model(parsed.model)
        return parsed

    async def list_models(self) -> list[str]:
        """Return models confirmed by successful calls for providers without discovery."""

        return sorted(self._observed_models)

    async def rate_limit(self) -> RateLimitSnapshot | None:
        return self._rate_limit

    async def account_info(self) -> ProviderAccountInfo:
        """Return normalized identity metadata from the provider CLI."""
        return ProviderAccountInfo(provider=self.name, status="unavailable")

    def observe_model(self, model: str) -> None:
        if model and model.lower() not in {"default", self.name}:
            self._observed_models.add(model)

    def environment_overrides(
        self, request: ChatCompletionRequest
    ) -> dict[str, str] | None:
        return None

    def accepts_model(self, model: str) -> bool:
        return uses_default_model(self.name, model) or model in self._observed_models

    def set_rate_limit(self, snapshot: RateLimitSnapshot | None) -> None:
        self._rate_limit = snapshot

    async def preflight(self) -> RateLimitSnapshot | None:
        snapshot = await self.rate_limit()
        if snapshot is not None and snapshot.exhausted:
            retry_after = snapshot.retry_after_seconds
            if retry_after is None and snapshot.resets_at is not None:
                retry_after = max(1, snapshot.resets_at - int(time.time()))
            raise ProviderRateLimitError(
                f"{self.name} subscription quota is exhausted",
                retry_after_seconds=retry_after,
            )
        return snapshot

    @abstractmethod
    def build_command(
        self, request: ChatCompletionRequest, cwd: Path | None = None
    ) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        raise NotImplementedError

    @abstractmethod
    def parse_output(self, output: str, requested_model: str) -> ProviderResult:
        raise NotImplementedError


def uses_default_model(provider: str, model: str) -> bool:
    return model.lower() in {"default", provider}


def write_output_schema(request: ChatCompletionRequest, cwd: Path) -> Path | None:
    """Write a provider schema inside the request's temporary directory."""

    import json

    schema = output_schema(request)
    if schema is None:
        return None
    path = cwd / "output-schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path
