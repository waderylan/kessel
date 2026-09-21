"""Provider lookup and concurrency control."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing

from app.models import ChatCompletionRequest, ProviderResult, ProviderStreamEvent
from app.rate_limits import RateLimitSnapshot
from app.providers.base import ProviderAdapter


class ProviderRegistry:
    def __init__(
        self,
        providers: Mapping[str, ProviderAdapter],
        max_concurrent_requests: int,
    ) -> None:
        self._providers = dict(providers)
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def get(self, name: str) -> ProviderAdapter:
        return self._providers[name]

    async def complete(
        self, provider_name: str, request: ChatCompletionRequest
    ) -> ProviderResult:
        async with self._semaphore:
            provider = self.get(provider_name)
            result = await provider.complete(request)
            provider.observe_model(result.model)
            return result

    async def stream(
        self, provider_name: str, request: ChatCompletionRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        async with self._semaphore:
            provider = self.get(provider_name)
            async with aclosing(provider.stream(request)) as provider_stream:
                async for event in provider_stream:
                    if event.result is not None:
                        provider.observe_model(event.result.model)
                    yield event

    async def list_models(self, provider_name: str) -> list[str]:
        async with self._semaphore:
            return await self.get(provider_name).list_models()

    async def preflight(self, provider_name: str) -> RateLimitSnapshot | None:
        return await self.get(provider_name).preflight()

    async def rate_limit(self, provider_name: str) -> RateLimitSnapshot | None:
        return await self.get(provider_name).rate_limit()

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
