"""Provider lookup, admission control, and graceful shutdown."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing, asynccontextmanager

from app.models import ChatCompletionRequest, ProviderResult, ProviderStreamEvent
from app.providers.base import ProviderAdapter
from app.rate_limits import RateLimitSnapshot
from app.runner import ProcessError, ProviderBusyError


class ProviderRegistry:
    def __init__(
        self,
        providers: Mapping[str, ProviderAdapter],
        max_concurrent_requests: int | Mapping[str, int],
        slot_wait_seconds: int = 5,
        shutdown_grace_seconds: int = 5,
    ) -> None:
        self._providers = dict(providers)
        if isinstance(max_concurrent_requests, int):
            limits = {
                name: max_concurrent_requests for name in self._providers
            }
        else:
            limits = dict(max_concurrent_requests)
        self._semaphores = {
            name: asyncio.Semaphore(limits[name]) for name in self._providers
        }
        self._slot_wait_seconds = slot_wait_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._inflight: set[asyncio.Task] = set()
        self._closing = False

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def get(self, name: str) -> ProviderAdapter:
        return self._providers[name]

    @asynccontextmanager
    async def _provider_slot(self, provider_name: str):
        if self._closing:
            raise ProcessError("Kessel is shutting down")
        semaphore = self._semaphores[provider_name]
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    semaphore.acquire(), timeout=self._slot_wait_seconds
                )
                acquired = True
            except asyncio.TimeoutError as exc:
                raise ProviderBusyError(
                    f"{provider_name} concurrency limit reached",
                    retry_after_seconds=max(
                        1, math.ceil(self._slot_wait_seconds)
                    ),
                ) from exc
            if self._closing:
                raise ProcessError("Kessel is shutting down")
            yield
        finally:
            if task is not None:
                self._inflight.discard(task)
            if acquired:
                semaphore.release()

    async def complete(
        self, provider_name: str, request: ChatCompletionRequest
    ) -> ProviderResult:
        async with self._provider_slot(provider_name):
            provider = self.get(provider_name)
            result = await provider.complete(request)
            provider.observe_model(result.model)
            return result

    async def stream(
        self, provider_name: str, request: ChatCompletionRequest
    ) -> AsyncIterator[ProviderStreamEvent]:
        async with self._provider_slot(provider_name):
            provider = self.get(provider_name)
            async with aclosing(provider.stream(request)) as provider_stream:
                async for event in provider_stream:
                    if event.result is not None:
                        provider.observe_model(event.result.model)
                    yield event

    async def list_models(self, provider_name: str) -> list[str]:
        async with self._provider_slot(provider_name):
            return await self.get(provider_name).list_models()

    async def accepts_model(self, provider_name: str, model: str) -> bool:
        provider = self.get(provider_name)
        if provider.accepts_model(model):
            return True
        if provider_name != "codex":
            return False
        async with self._provider_slot(provider_name):
            return model in await provider.list_models()

    async def preflight(self, provider_name: str) -> RateLimitSnapshot | None:
        if self._closing:
            raise ProcessError("Kessel is shutting down")
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)
        try:
            return await self.get(provider_name).preflight()
        finally:
            if task is not None:
                self._inflight.discard(task)

    async def rate_limit(self, provider_name: str) -> RateLimitSnapshot | None:
        if self._closing:
            raise ProcessError("Kessel is shutting down")
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)
        try:
            return await self.get(provider_name).rate_limit()
        finally:
            if task is not None:
                self._inflight.discard(task)

    async def close(self) -> None:
        """Drain active requests, cancel stragglers, and reap provider children."""

        self._closing = True
        current = asyncio.current_task()
        active = {
            task
            for task in self._inflight
            if task is not current and not task.done()
        }
        pending: set[asyncio.Task] = set()
        if active:
            _, pending = await asyncio.wait(
                active, timeout=self._shutdown_grace_seconds
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1)

        await asyncio.gather(
            *(provider.close() for provider in self._providers.values()),
            return_exceptions=True,
        )
        runners = {
            provider.runner for provider in self._providers.values()
        }
        await asyncio.gather(
            *(runner.terminate_all() for runner in runners),
            return_exceptions=True,
        )
        unfinished = {task for task in pending if not task.done()}
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.wait(unfinished, timeout=1)
