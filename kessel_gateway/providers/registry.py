"""Provider lookup, admission control, and graceful shutdown."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import aclosing, asynccontextmanager

from kessel_gateway.models import (
    ChatCompletionRequest,
    ProviderAccountInfo,
    ProviderResult,
    ProviderStreamEvent,
)
from kessel_gateway.providers.base import ProviderAdapter
from kessel_gateway.rate_limits import RateLimitSnapshot
from kessel_gateway.runner import (
    ProcessError,
    ProcessNotFoundError,
    ProviderBusyError,
    ProviderCompatibilityError,
)


MODEL_CACHE_TTL_SECONDS = 300.0
MODEL_CACHE_REFRESH_SECONDS = 30.0


class _ModelListCache:
    """Cache one provider's discovered model list for a bounded time."""

    def __init__(self) -> None:
        self._models: list[str] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    def get(self) -> list[str] | None:
        return self._models

    def age_seconds(self) -> float:
        return time.monotonic() - self._fetched_at

    async def refresh(
        self,
        fetch: Callable[[], Awaitable[list[str]]],
        max_age_seconds: float = MODEL_CACHE_TTL_SECONDS,
    ) -> list[str]:
        async with self._lock:
            if self._models is not None and self.age_seconds() < max_age_seconds:
                return self._models
            models = await fetch()
            self._models = models
            self._fetched_at = time.monotonic()
            return models


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
        self._model_caches = {
            name: _ModelListCache() for name in self._providers
        }
        self._inflight: dict[object, asyncio.Future] = {}
        self._closing = False
        self._disabled: dict[str, str] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def get(self, name: str) -> ProviderAdapter:
        if name in self._disabled:
            raise ProviderCompatibilityError(name, self._disabled[name])
        return self._providers[name]

    def command(self, name: str) -> str:
        return self._providers[name].command

    def disable(self, reasons: Mapping[str, str]) -> None:
        self._disabled.update(reasons)

    def is_enabled(self, name: str) -> bool:
        return name not in self._disabled

    def _begin_inflight(self) -> object:
        token = object()
        self._inflight[token] = asyncio.get_running_loop().create_future()
        return token

    def _end_inflight(self, token: object) -> None:
        future = self._inflight.pop(token, None)
        if future is not None and not future.done():
            future.set_result(None)

    @asynccontextmanager
    async def _tracked(self):
        """Track a unit of work for shutdown draining without limiting concurrency."""

        if self._closing:
            raise ProcessError("Kessel is shutting down")
        token = self._begin_inflight()
        try:
            yield
        finally:
            self._end_inflight(token)

    async def _acquire_slot(self, provider_name: str, semaphore: asyncio.Semaphore) -> None:
        """Acquire a concurrency slot with a cancellation-safe deadline.

        Avoids ``asyncio.wait_for(semaphore.acquire(), ...)``, which on Python
        3.10/3.11 can leave a cancelled acquire's permit stranded if the
        wrapping task is cancelled at the wrong moment. Using a plain
        ``asyncio.wait`` over a task we own, and explicitly reconciling that
        task's outcome afterward, avoids that race.
        """

        acquire_task = asyncio.ensure_future(semaphore.acquire())
        try:
            done, _ = await asyncio.wait(
                {acquire_task}, timeout=self._slot_wait_seconds
            )
        except asyncio.CancelledError:
            await self._abort_acquire(acquire_task, semaphore)
            raise
        if acquire_task in done and not acquire_task.cancelled():
            return
        await self._abort_acquire(acquire_task, semaphore)
        raise ProviderBusyError(
            f"{provider_name} concurrency limit reached",
            retry_after_seconds=max(1, math.ceil(self._slot_wait_seconds)),
        )

    @staticmethod
    async def _abort_acquire(
        acquire_task: asyncio.Task, semaphore: asyncio.Semaphore
    ) -> None:
        """Cancel a pending acquire, or release its permit if it slipped through."""

        if not acquire_task.done():
            acquire_task.cancel()
        try:
            await acquire_task
        except (asyncio.CancelledError, Exception):
            return
        semaphore.release()

    @asynccontextmanager
    async def _provider_slot(self, provider_name: str):
        if self._closing:
            raise ProcessError("Kessel is shutting down")
        semaphore = self._semaphores[provider_name]
        token = self._begin_inflight()
        acquired = False
        try:
            await self._acquire_slot(provider_name, semaphore)
            acquired = True
            if self._closing:
                raise ProcessError("Kessel is shutting down")
            yield
        finally:
            self._end_inflight(token)
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

    async def _refresh_models(
        self,
        provider_name: str,
        max_age_seconds: float = MODEL_CACHE_TTL_SECONDS,
    ) -> list[str]:
        provider = self.get(provider_name)
        return await self._model_caches[provider_name].refresh(
            provider.list_models, max_age_seconds
        )

    async def list_models(self, provider_name: str) -> list[str]:
        async with self._tracked():
            cache = self._model_caches[provider_name]
            cached = cache.get()
            if cached is not None and cache.age_seconds() < MODEL_CACHE_TTL_SECONDS:
                return cached
            return await self._refresh_models(provider_name)

    async def account_info(self, provider_name: str) -> ProviderAccountInfo:
        async with self._tracked():
            return await self.get(provider_name).account_info()

    async def account_infos(self) -> list[ProviderAccountInfo]:
        results = await asyncio.gather(
            *(self.account_info(name) for name in self.names),
            return_exceptions=True,
        )
        accounts: list[ProviderAccountInfo] = []
        for name, result in zip(self.names, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, ProcessNotFoundError):
                accounts.append(
                    ProviderAccountInfo(provider=name, status="not_installed")
                )
            elif isinstance(result, Exception):
                accounts.append(
                    ProviderAccountInfo(provider=name, status="unavailable")
                )
            elif isinstance(result, BaseException):
                raise result
            else:
                accounts.append(result)
        return accounts

    async def accepts_model(self, provider_name: str, model: str) -> bool:
        provider = self.get(provider_name)
        if provider.accepts_model(model):
            return True
        if provider_name != "codex":
            return False
        cache = self._model_caches[provider_name]
        cached = cache.get()
        if cached is not None and model in cached:
            return True
        if cached is not None and cache.age_seconds() < MODEL_CACHE_REFRESH_SECONDS:
            return False
        # An unknown model may be newly released, so refetch any list older
        # than the short refresh interval instead of trusting the full TTL.
        async with self._tracked():
            refreshed = await self._refresh_models(
                provider_name, MODEL_CACHE_REFRESH_SECONDS
            )
        return model in refreshed

    async def preflight(self, provider_name: str) -> RateLimitSnapshot | None:
        async with self._tracked():
            return await self.get(provider_name).preflight()

    async def rate_limit(self, provider_name: str) -> RateLimitSnapshot | None:
        async with self._tracked():
            return await self.get(provider_name).rate_limit()

    async def close(self) -> None:
        """Drain active requests, cancel stragglers, and reap provider children."""

        self._closing = True
        pending_futures = [
            future for future in self._inflight.values() if not future.done()
        ]
        if pending_futures:
            await asyncio.wait(pending_futures, timeout=self._shutdown_grace_seconds)

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
        unfinished = [
            future for future in self._inflight.values() if not future.done()
        ]
        if unfinished:
            await asyncio.wait(unfinished, timeout=1)
