"""FastAPI application and OpenAI-compatible HTTP routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import shutil
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from kessel_gateway import __version__
from kessel_gateway.config import Settings, settings
from kessel_gateway.models import (
    AnthropicMessagesRequest,
    ChatCompletionRequest,
    ChatCompletionResponse,
    CompletionChoice,
    CompletionMessage,
    ErrorDetail,
    ErrorResponse,
    ProviderAccountsResponse,
    ProviderResult,
)
from kessel_gateway.output_control import control_output_stream
from kessel_gateway.providers.claude import ClaudeProvider
from kessel_gateway.providers.codex import CodexProvider
from kessel_gateway.providers.compatibility import known_stable_guidance
from kessel_gateway.providers.registry import ProviderRegistry
from kessel_gateway.runner import (
    ProcessError,
    ProcessExitError,
    ProcessNotFoundError,
    ProcessOutputLimitError,
    ProcessRunner,
    ProcessTimeoutError,
    ProviderAuthenticationError,
    ProviderBusyError,
    ProviderCompatibilityError,
    ProviderRateLimitError,
)
from kessel_gateway.versioning import verify_cli_versions
from kessel_gateway.user_config import UserConfig


STATIC_DIRECTORY = Path(__file__).parent / "static"
REQUEST_LOGGER = logging.getLogger("uvicorn.error")
REQUEST_LOGGER.setLevel(logging.INFO)
HEALTH_CACHE_TTL_SECONDS = 5.0


class ConfigApiKeyCache:
    """Cache the file-backed API key until the atomic config file changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded = False
        self._path: Path | None = None
        self._signature: tuple[int, int, int, int] | None = None
        self._key: str | None = None

    def get(self) -> str | None:
        path = UserConfig().path
        with self._lock:
            try:
                stat = path.stat()
                signature: tuple[int, int, int, int] | None = (
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                    stat.st_size,
                    stat.st_ino,
                )
            except FileNotFoundError:
                signature = None
            except OSError as exc:
                raise ValueError(
                    f"Could not inspect Kessel config at {path}: {exc}"
                ) from exc
            if (
                not self._loaded
                or path != self._path
                or signature != self._signature
            ):
                key = UserConfig.load().api_key
                self._loaded = True
                self._path = path
                self._signature = signature
                self._key = key
            return self._key


class ProviderAvailabilityCache:
    """Cache provider PATH resolution for a short, refreshable interval."""

    def __init__(self, ttl_seconds: float = HEALTH_CACHE_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._expires_at = 0.0
        self._commands: tuple[tuple[str, str], ...] = ()
        self._status: dict[str, dict[str, bool]] = {}
        self._lock = asyncio.Lock()

    async def get(
        self, commands: tuple[tuple[str, str], ...]
    ) -> dict[str, dict[str, bool]]:
        now = time.monotonic()
        if commands == self._commands and now < self._expires_at:
            return self._status
        async with self._lock:
            now = time.monotonic()
            if commands == self._commands and now < self._expires_at:
                return self._status
            executables = await asyncio.gather(
                *(asyncio.to_thread(shutil.which, command) for _, command in commands)
            )
            self._commands = commands
            self._status = {
                name: {"available": executable is not None}
                for (name, _), executable in zip(commands, executables, strict=True)
            }
            self._expires_at = now + self._ttl_seconds
            return self._status


_ANTHROPIC_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    529: "overloaded_error",
}
_OPENAI_ERROR_TYPES = {
    401: "authentication_error",
    403: "permission_error",
    429: "rate_limit_error",
}


def build_error_content(
    is_anthropic: bool,
    status_code: int,
    message: str,
    code: str,
    request_id: str,
    param: str | None = None,
) -> dict[str, object]:
    """Build the OpenAI or Anthropic error body shape for a rejected request."""

    if is_anthropic:
        return {
            "type": "error",
            "error": {
                "type": _ANTHROPIC_ERROR_TYPES.get(status_code, "api_error"),
                "message": message,
            },
            "request_id": request_id,
        }
    error_type = _OPENAI_ERROR_TYPES.get(
        status_code,
        "invalid_request_error" if 400 <= status_code < 500 else "server_error",
    )
    return ErrorResponse(
        error=ErrorDetail(message=message, type=error_type, code=code, param=param)
    ).model_dump()


class LocalHTTPGuard:
    """Pure ASGI middleware: host/origin/size checks, request ids, and JSON logging.

    Replaces the previous pair of ``BaseHTTPMiddleware`` handlers with a single
    ASGI-level middleware so a streamed body can be buffered once and replayed
    to the application without breaking ``http.disconnect`` delivery.
    """

    def __init__(self, app, app_settings: Settings) -> None:
        self.app = app
        self.settings = app_settings

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = f"req_local_{uuid.uuid4().hex}"
        scope.setdefault("state", {})["request_id"] = request_id
        path = scope.get("path", "")
        method = scope.get("method", "")
        is_anthropic_path = path == "/v1/messages"
        header_values: dict[bytes, bytes] = {}
        for key, value in scope.get("headers") or ():
            header_values.setdefault(key, value)

        def header(name: bytes) -> str | None:
            value = header_values.get(name)
            return value.decode("latin-1") if value is not None else None

        async def reject(status_code: int, message: str, code: str) -> None:
            content = build_error_content(
                is_anthropic_path, status_code, message, code, request_id
            )
            headers = {"x-request-id": request_id}
            if is_anthropic_path:
                headers["request-id"] = request_id
            response = JSONResponse(
                status_code=status_code, content=content, headers=headers
            )
            await response(scope, receive, send)

        allowed_hosts = {
            f"127.0.0.1:{self.settings.listen_port}",
            f"localhost:{self.settings.listen_port}",
            f"[::1]:{self.settings.listen_port}",
        }
        host = (header(b"host") or "").lower()
        if host not in allowed_hosts:
            await reject(421, "Invalid Host header", "invalid_host")
            return

        origin = header(b"origin")
        if origin is not None and origin not in self.settings.cors_origins:
            await reject(403, "Origin is not allowed", "origin_not_allowed")
            return

        if method == "POST" and path.startswith("/v1/"):
            media_type = (
                (header(b"content-type") or "").split(";", 1)[0].strip().lower()
            )
            if media_type != "application/json":
                await reject(
                    415,
                    "Content-Type must be application/json",
                    "unsupported_media_type",
                )
                return

        content_length_header = header(b"content-length")
        if content_length_header is not None:
            try:
                declared_length = int(content_length_header)
            except ValueError:
                await reject(400, "Invalid Content-Length header", "invalid_request")
                return
            if declared_length < 0:
                await reject(400, "Invalid Content-Length header", "invalid_request")
                return
            if declared_length > self.settings.max_request_bytes:
                await reject(413, "Request body is too large", "request_too_large")
                return

        receive_for_app = receive
        if method in {"POST", "PUT", "PATCH"}:
            body = bytearray()
            oversized = False
            disconnect_message: dict | None = None
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnect_message = message
                    break
                body.extend(message.get("body", b""))
                if len(body) > self.settings.max_request_bytes:
                    oversized = True
                    break
                if not message.get("more_body", False):
                    break
            if oversized:
                await reject(413, "Request body is too large", "request_too_large")
                return

            buffered_body = bytes(body)
            replayed = False
            disconnect_sent = disconnect_message is None

            async def buffered_receive():
                nonlocal replayed, disconnect_sent
                if not replayed:
                    replayed = True
                    return {
                        "type": "http.request",
                        "body": buffered_body,
                        "more_body": False,
                    }
                if not disconnect_sent:
                    disconnect_sent = True
                    return disconnect_message
                return await receive()

            receive_for_app = buffered_receive

        started_at = time.monotonic()
        REQUEST_LOGGER.info(
            json.dumps(
                {
                    "event": "request.started",
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                },
                separators=(",", ":"),
            )
        )
        response_state: dict[str, int] = {}

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                response_state["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                if is_anthropic_path:
                    headers.append((b"request-id", request_id.encode("latin-1")))
                if path == "/" or path.startswith("/static/"):
                    headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        def log(event: str) -> None:
            REQUEST_LOGGER.info(
                json.dumps(
                    {
                        "event": event,
                        "request_id": request_id,
                        "method": method,
                        "path": path,
                        "duration_ms": round(
                            (time.monotonic() - started_at) * 1000, 1
                        ),
                    },
                    separators=(",", ":"),
                )
            )

        try:
            await self.app(scope, receive_for_app, send_wrapper)
        except asyncio.CancelledError:
            log("request.cancelled")
            raise
        except BaseException:
            event = "request.stream_failed" if response_state else "request.failed"
            REQUEST_LOGGER.exception(
                json.dumps(
                    {
                        "event": event,
                        "request_id": request_id,
                        "method": method,
                        "path": path,
                        "duration_ms": round(
                            (time.monotonic() - started_at) * 1000, 1
                        ),
                    },
                    separators=(",", ":"),
                )
            )
            raise
        else:
            REQUEST_LOGGER.info(
                json.dumps(
                    {
                        "event": "request.completed",
                        "request_id": request_id,
                        "method": method,
                        "path": path,
                        "status": response_state.get("status"),
                        "duration_ms": round(
                            (time.monotonic() - started_at) * 1000, 1
                        ),
                    },
                    separators=(",", ":"),
                )
            )


async def _watch_for_disconnect(raw_request: Request, task: "asyncio.Task[Any]") -> None:
    """Poll for client disconnect and cancel the tracked task when it happens."""

    while not task.done():
        if await raw_request.is_disconnected():
            task.cancel()
            return
        await asyncio.sleep(0.05)


class GuardedStream:
    """Iterate an async source behind one disconnect watcher for the whole request.

    A single producer task pulls from ``source`` and puts items on a small
    queue; a single watcher task polls ``request.is_disconnected()`` and
    cancels the producer when the client goes away. This replaces creating a
    fresh watcher (and polling loop) for every streamed event.
    """

    def __init__(self, raw_request: Request, source: AsyncIterator[Any]) -> None:
        self._source = source
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._delivered_first = False
        self._closed = False
        self._producer_task = asyncio.create_task(self._produce())
        self._watcher_task = asyncio.create_task(
            _watch_for_disconnect(raw_request, self._producer_task)
        )

    async def _produce(self) -> None:
        try:
            async for event in self._source:
                await self._queue.put(("event", event))
            await self._queue.put(("done", None))
        except asyncio.CancelledError:
            # Never block here: after a disconnect or aclose() nobody may be
            # reading, and a blocked put would strand the provider process.
            if not self._closed:
                self._offer(("disconnected", None))
            raise
        except Exception as exc:  # forwarded to the consumer below
            await self._queue.put(("error", exc))
        finally:
            # Close the source from the task that iterated it, so provider
            # cleanup runs even if the caller of aclose() is itself cancelled.
            close = getattr(self._source, "aclose", None)
            if close is not None:
                await close()

    def _offer(self, item: tuple[str, Any]) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

    def __aiter__(self) -> "GuardedStream":
        return self

    async def __anext__(self) -> Any:
        kind, payload = await self._queue.get()
        if kind == "event":
            self._delivered_first = True
            return payload
        if kind == "error":
            raise payload
        if kind == "disconnected" and not self._delivered_first:
            raise HTTPException(status_code=499, detail="Client disconnected")
        raise StopAsyncIteration

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._producer_task.cancel()
        self._watcher_task.cancel()
        await asyncio.gather(
            self._producer_task, self._watcher_task, return_exceptions=True
        )
        # Covers a producer cancelled before it ever started running; closing
        # an already-closed async generator is a no-op.
        close = getattr(self._source, "aclose", None)
        if close is not None:
            await close()


async def _single_result(awaitable: Any) -> AsyncIterator[Any]:
    yield await awaitable


def create_registry(app_settings: Settings) -> ProviderRegistry:
    runner = ProcessRunner(
        timeout_seconds=app_settings.request_timeout_seconds,
        max_output_bytes=app_settings.max_output_bytes,
    )
    return ProviderRegistry(
        {
            "codex": CodexProvider(app_settings.codex_command, runner),
            "claude": ClaudeProvider(app_settings.claude_command, runner),
        },
        max_concurrent_requests={
            "codex": app_settings.provider_limit("codex"),
            "claude": app_settings.provider_limit("claude"),
        },
        slot_wait_seconds=app_settings.provider_slot_wait_seconds,
        shutdown_grace_seconds=app_settings.shutdown_grace_seconds,
    )


def create_app(
    app_settings: Settings = settings,
    registry: ProviderRegistry | None = None,
) -> FastAPI:
    owns_registry = registry is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if owns_registry and app_settings.enforce_cli_versions:
            report = await verify_cli_versions(app_settings)
            app.state.cli_versions = report.compatible
            app.state.registry.disable(report.incompatible)
        yield
        close = getattr(app.state.registry, "close", None)
        if close is not None:
            await close()

    application = FastAPI(
        title="Kessel Local API",
        version=__version__,
        description=(
            "Stateless OpenAI-compatible Chat Completions over local Codex and "
            "Claude Code subscriptions."
        ),
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.state.registry = registry or create_registry(app_settings)
    application.state.cli_versions = {}
    application.state.api_key_cache = ConfigApiKeyCache()
    application.state.provider_availability_cache = ProviderAvailabilityCache()

    if app_settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(app_settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-API-Key",
                "Anthropic-Version",
                "X-Request-ID",
            ],
            expose_headers=[
                "request-id",
                "x-request-id",
                "retry-after",
                "ratelimit-limit",
                "ratelimit-remaining",
                "ratelimit-reset",
                "x-kessel-quota-remaining-percent",
                "x-kessel-quota-reset-at",
                "x-kessel-model-discovery",
            ],
        )

    application.add_middleware(LocalHTTPGuard, app_settings=app_settings)

    async def require_api_key(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> None:
        expected_key = application.state.settings.api_key
        if application.state.settings.reload_api_key_from_config:
            try:
                expected_key = await asyncio.to_thread(
                    application.state.api_key_cache.get
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Kessel configuration is invalid",
                ) from exc
        if not expected_key:
            if application.state.settings.allow_unauthenticated:
                return
            raise HTTPException(
                status_code=503,
                detail="Kessel has no API key. Run: kessel setup",
            )
        supplied_key = ""
        if authorization:
            parts = authorization.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                supplied_key = parts[1]
        if not supplied_key and x_api_key:
            supplied_key = x_api_key
        supplied_digest = hashlib.sha256(supplied_key.encode("utf-8")).digest()
        expected_digest = hashlib.sha256(expected_key.encode("utf-8")).digest()
        if not secrets.compare_digest(supplied_digest, expected_digest):
            raise HTTPException(
                status_code=401,
                detail=(
                    "Wrong or missing API key. Send it in Authorization: Bearer "
                    "<key> or X-API-Key. Run kessel key to see yours"
                ),
            )

    def error_content(
        message: str, error_type: str, code: str, param: str | None = None
    ) -> dict[str, object]:
        return ErrorResponse(
            error=ErrorDetail(
                message=message, type=error_type, code=code, param=param
            )
        ).model_dump()

    def is_anthropic(request: Request) -> bool:
        return request.url.path == "/v1/messages"

    def error_response(
        request: Request,
        status_code: int,
        message: str,
        code: str,
        *,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        content = build_error_content(
            is_anthropic(request),
            status_code,
            message,
            code,
            request.state.request_id,
            param,
        )
        return JSONResponse(status_code=status_code, content=content, headers=headers)

    @application.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        message = (
            exc.detail
            if isinstance(exc.detail, str)
            else detail.get("message", "Request failed")
        )
        return error_response(
            request,
            exc.status_code,
            message,
            detail.get("code")
            or {
                400: "invalid_request",
                401: "invalid_api_key",
                403: "permission_denied",
                404: "not_found",
            }.get(exc.status_code, "http_error"),
            param=detail.get("param"),
            headers=exc.headers,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first_error = exc.errors()[0] if exc.errors() else {}
        location = first_error.get("loc", ())
        parameter = ".".join(str(part) for part in location if part != "body") or None
        message = first_error.get("msg", "Invalid request")
        return error_response(
            request,
            400,
            message,
            "validation_error",
            param=parameter,
        )

    @application.exception_handler(ProcessError)
    async def provider_error_handler(request: Request, exc: ProcessError) -> JSONResponse:
        explicit_status = getattr(exc, "status_code", None)
        explicit_code = getattr(exc, "error_code", None)
        if explicit_status is not None or explicit_code is not None:
            explicit_message = getattr(exc, "public_message", None)
            return error_response(
                request,
                explicit_status or 500,
                explicit_message or "Provider request failed",
                explicit_code or "provider_error",
            )

        provider_name = (
            "claude"
            if request.url.path == "/v1/messages" or "/claude/" in request.url.path
            else "codex"
        )
        process_auth_error = (
            isinstance(exc, ProcessExitError)
            and any(
                marker in exc.stderr.lower()
                for marker in (
                    "not logged in",
                    "not authenticated",
                    "authentication required",
                    "please login",
                    "please log in",
                    "run /login",
                )
            )
        )
        if isinstance(exc, ProviderBusyError):
            status_code, code = 429, "provider_busy"
        elif isinstance(exc, ProviderAuthenticationError) or process_auth_error:
            status_code, code = 503, "provider_not_authenticated"
        elif isinstance(exc, ProviderCompatibilityError):
            status_code, code = 503, "provider_incompatible"
        elif isinstance(exc, ProviderRateLimitError):
            status_code, code = 429, "rate_limit_exceeded"
        elif isinstance(exc, ProcessNotFoundError):
            status_code, code = 503, "provider_not_installed"
        elif isinstance(exc, ProcessTimeoutError):
            status_code, code = 504, "provider_timeout"
        elif isinstance(exc, ProcessOutputLimitError):
            status_code, code = 502, "provider_output_limit"
        elif isinstance(exc, ProcessExitError):
            stderr = exc.stderr.lower()
            if any(marker in stderr for marker in ("rate limit", "rate_limit", "usage limit")):
                status_code, code = 429, "rate_limit_exceeded"
            else:
                status_code, code = 502, "provider_process_failed"
        else:
            status_code, code = 502, "provider_error"

        message = {
            "provider_busy": "Provider concurrency limit reached",
            "rate_limit_exceeded": "Provider subscription quota is exhausted",
            "provider_not_installed": "Provider command is not installed",
            "provider_incompatible": (
                "Provider CLI version or capabilities are incompatible. "
                "Run: kessel doctor"
            ),
            "provider_timeout": "Provider request timed out",
            "provider_output_limit": "Provider output exceeded the configured limit",
            "provider_process_failed": "Provider process failed",
            "provider_error": "Provider request failed",
        }.get(code, "Provider request failed")
        if isinstance(exc, ProviderAuthenticationError) or process_auth_error:
            auth_provider = (
                exc.provider
                if isinstance(exc, ProviderAuthenticationError)
                else provider_name
            )
            display = "Claude Code" if auth_provider == "claude" else "Codex"
            command = "claude login" if auth_provider == "claude" else "codex login"
            message = f"{display} isn't logged in. Run: {command}"
        elif isinstance(exc, ProviderCompatibilityError):
            message = (
                f"{exc.provider} CLI is incompatible: {exc.reason}. "
                f"{known_stable_guidance(exc.provider)}"
            )
        elif isinstance(exc, ProviderRateLimitError) and exc.retry_after_seconds:
            reset_at = datetime.fromtimestamp(
                time.time() + exc.retry_after_seconds
            ).astimezone()
            message = (
                f"{message}. Quota resets at "
                f"{reset_at.isoformat(timespec='minutes')}"
            )
        headers = None
        if isinstance(exc, ProviderBusyError):
            retry_after = str(max(1, exc.retry_after_seconds or 1))
            headers = {"retry-after": retry_after}
        elif isinstance(exc, ProviderRateLimitError):
            headers = {
                "ratelimit-limit": "100",
                "ratelimit-remaining": "0",
                "x-kessel-quota-remaining-percent": "0",
            }
            if exc.retry_after_seconds:
                retry_after = str(max(1, exc.retry_after_seconds))
                headers["retry-after"] = retry_after
                headers["ratelimit-reset"] = retry_after
        return error_response(
            request, status_code, message, code, headers=headers
        )

    @application.get("/health")
    async def health() -> JSONResponse:
        command_for = getattr(
            application.state.registry,
            "command",
            lambda name: application.state.registry.get(name).command,
        )
        is_enabled = getattr(
            application.state.registry,
            "is_enabled",
            lambda name: True,
        )
        commands = tuple(
            (name, command_for(name))
            for name in application.state.registry.names
        )
        provider_status = await application.state.provider_availability_cache.get(
            commands
        )
        provider_status = {
            name: {
                "available": status["available"]
                and is_enabled(name)
            }
            for name, status in provider_status.items()
        }
        degraded = not any(
            status["available"] for status in provider_status.values()
        )
        content: dict[str, object] = {
            "service": "kessel",
            "status": "degraded" if degraded else "ok",
            "providers": provider_status,
        }
        if degraded:
            content["message"] = (
                "No supported provider CLI is installed. Install Codex "
                "or Claude Code, then run `kessel setup`."
            )
        return JSONResponse(content=content)

    def unsupported_parameter(parameter: str, reason: str) -> None:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"Unsupported parameter '{parameter}': {reason}",
                "param": parameter,
                "code": "unsupported_parameter",
            },
        )

    def validate_chat_surface(body: ChatCompletionRequest) -> None:
        if body.parallel_tool_calls:
            unsupported_parameter(
                "parallel_tool_calls",
                "parallel function calls are not supported",
            )
        stop_sequences = chat_stop_sequences(body)
        structured_response = (
            body.response_format is not None
            and body.response_format.type != "text"
        )
        if stop_sequences and (structured_response or body.tools):
            unsupported_parameter(
                "stop",
                "stop sequences are not supported with structured output or tools",
            )
        if body.n != 1:
            unsupported_parameter("n", "only n=1 is supported")

    def chat_stop_sequences(body: ChatCompletionRequest) -> tuple[str, ...]:
        if isinstance(body.stop, str):
            return (body.stop,)
        return tuple(body.stop or ())

    def chat_token_limit(body: ChatCompletionRequest) -> int | None:
        limits = [
            limit
            for limit in (body.max_tokens, body.max_completion_tokens)
            if limit is not None
        ]
        return min(limits) if limits else None

    def controlled_provider_stream(
        provider: str,
        body: ChatCompletionRequest,
    ):
        source = application.state.registry.stream(provider, body)
        token_limit = chat_token_limit(body)
        stop_sequences = chat_stop_sequences(body)
        if token_limit is None and not stop_sequences:
            return source
        return control_output_stream(
            source,
            requested_model=body.model,
            max_tokens=token_limit,
            stop_sequences=stop_sequences,
        )

    async def controlled_completion(
        provider: str,
        body: ChatCompletionRequest,
    ) -> ProviderResult:
        if chat_token_limit(body) is None and not chat_stop_sequences(body):
            return await application.state.registry.complete(provider, body)

        final_result: ProviderResult | None = None
        stream = controlled_provider_stream(provider, body)
        try:
            async for event in stream:
                if event.result is not None:
                    final_result = event.result
        finally:
            await stream.aclose()
        if final_result is None:
            raise ProcessError("provider stream ended without a result")
        return final_result

    async def await_with_disconnect_guard(operation, raw_request: Request):
        """Await a single coroutine behind one disconnect watcher for the request."""

        guarded = GuardedStream(raw_request, _single_result(operation))
        try:
            return await anext(guarded)
        finally:
            await guarded.aclose()

    async def preflight_headers(provider: str) -> dict[str, str]:
        snapshot = await application.state.registry.preflight(provider)
        return snapshot.headers() if snapshot is not None else {}

    async def current_rate_limit_headers(provider: str) -> dict[str, str]:
        snapshot = await application.state.registry.rate_limit(provider)
        return snapshot.headers() if snapshot is not None else {}

    @application.get(
        "/v1/providers/accounts",
        response_model=ProviderAccountsResponse,
        dependencies=[Depends(require_api_key)],
    )
    async def provider_accounts(response: Response) -> ProviderAccountsResponse:
        accounts = await application.state.registry.account_infos()
        response.headers["cache-control"] = "no-store"
        return ProviderAccountsResponse(data=accounts)

    @application.get("/v1/{provider}/models", dependencies=[Depends(require_api_key)])
    async def list_models(provider: str, response: Response) -> dict[str, object]:
        if provider not in application.state.registry.names:
            raise HTTPException(status_code=404, detail="Unknown provider")
        model_ids = await application.state.registry.list_models(provider)
        response.headers["x-kessel-model-discovery"] = (
            "provider" if provider == "codex" else "confirmed-this-process"
        )
        if provider == "codex":
            for name, value in (await current_rate_limit_headers(provider)).items():
                response.headers[name] = value
        return {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": provider,
                }
                for model_id in model_ids
            ],
        }

    @application.post(
        "/v1/{provider}/chat/completions",
        response_model=ChatCompletionResponse,
        responses={400: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
        dependencies=[Depends(require_api_key)],
    )
    async def create_chat_completion(
        provider: str,
        body: ChatCompletionRequest,
        raw_request: Request,
    ) -> Response:
        if provider not in application.state.registry.names:
            raise HTTPException(status_code=404, detail="Unknown provider")
        if not await application.state.registry.accepts_model(provider, body.model):
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Unknown or unapproved model",
                    "param": "model",
                    "code": "invalid_model",
                },
            )
        validate_chat_surface(body)
        headers = (
            await preflight_headers(provider)
            if provider != "codex" or body.backend == "warm"
            else {}
        )
        if body.backend == "warm" and provider != "codex":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Warm mode is only available for Codex. Claude stream-json "
                    "keeps conversation state and cannot provide stateless requests."
                ),
            )
        if body.stream:
            completion_id = f"chatcmpl-local-{uuid.uuid4().hex}"
            created = int(time.time())
            guarded = GuardedStream(raw_request, controlled_provider_stream(provider, body))
            try:
                first_provider_event = await anext(guarded)
            except StopAsyncIteration as exc:
                await guarded.aclose()
                raise ProcessError("provider stream ended without a result") from exc
            except BaseException:
                await guarded.aclose()
                raise

            async def event_stream():
                emitted_content = False

                def encode(payload: object) -> str:
                    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"

                def chunk(delta: dict[str, object], finish_reason=None) -> dict:
                    payload = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": body.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": delta,
                                "logprobs": None,
                                "finish_reason": finish_reason,
                            }
                        ],
                    }
                    if body.stream_options and body.stream_options.include_usage:
                        payload["usage"] = None
                    return payload

                yield encode(chunk({"role": "assistant", "content": ""}))
                try:
                    async def provider_events():
                        yield first_provider_event
                        async for event in guarded:
                            yield event

                    async for event in provider_events():
                        if event.delta:
                            emitted_content = True
                            yield encode(chunk({"content": event.delta}))
                        if event.result is None:
                            continue
                        result = event.result
                        if result.tool_calls:
                            tool = result.tool_calls[0]
                            yield encode(
                                chunk(
                                    {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                **tool.model_dump(),
                                            }
                                        ]
                                    }
                                )
                            )
                            finish_reason = result.finish_reason or "tool_calls"
                        else:
                            if result.text and not emitted_content:
                                yield encode(chunk({"content": result.text}))
                            finish_reason = result.finish_reason or "stop"
                        yield encode(chunk({}, finish_reason))
                        if (
                            body.stream_options
                            and body.stream_options.include_usage
                        ):
                            usage_chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": result.model,
                                "choices": [],
                                "usage": (
                                    result.usage.model_dump()
                                    if result.usage is not None
                                    else None
                                ),
                            }
                            yield encode(usage_chunk)
                except ProcessError as exc:
                    rate_limited = isinstance(exc, ProviderRateLimitError)
                    message = getattr(exc, "public_message", None) or (
                        "Provider subscription quota is exhausted"
                        if rate_limited
                        else "Provider stream failed"
                    )
                    yield encode(
                        error_content(
                            message,
                            "rate_limit_error" if rate_limited else "server_error",
                            "rate_limit_exceeded" if rate_limited else "stream_error",
                        )
                    )
                finally:
                    await guarded.aclose()
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    **headers,
                },
                background=BackgroundTask(guarded.aclose),
            )

        result = await await_with_disconnect_guard(
            controlled_completion(provider, body), raw_request
        )
        finish_reason = (
            result.finish_reason
            or ("tool_calls" if result.tool_calls else "stop")
        )
        payload = ChatCompletionResponse(
            id=f"chatcmpl-local-{uuid.uuid4().hex}",
            created=int(time.time()),
            model=result.model,
            choices=[
                CompletionChoice(
                    message=CompletionMessage(
                        content=result.text,
                        tool_calls=result.tool_calls,
                    ),
                    finish_reason=finish_reason,
                )
            ],
            usage=result.usage,
        )
        if body.backend == "warm":
            headers.update(await current_rate_limit_headers(provider))
        return JSONResponse(content=payload.model_dump(), headers=headers)

    @application.post(
        "/v1/messages",
        dependencies=[Depends(require_api_key)],
        responses={400: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    )
    async def create_anthropic_message(
        body: AnthropicMessagesRequest,
        raw_request: Request,
    ) -> Response:
        if body.stop_sequences and body.tools:
            unsupported_parameter(
                "stop_sequences",
                "stop sequences are not supported with tools",
            )
        headers = await preflight_headers("claude")
        if body.backend == "warm":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Warm Claude mode is unavailable because one stream-json "
                    "process retains conversation state across turns."
                ),
            )
        try:
            chat_request = body.to_chat_request()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not await application.state.registry.accepts_model(
            "claude", chat_request.model
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Unknown or unapproved model",
                    "param": "model",
                    "code": "invalid_model",
                },
            )

        message_id = f"msg_local_{uuid.uuid4().hex}"
        if body.stream:
            guarded = GuardedStream(
                raw_request, controlled_provider_stream("claude", chat_request)
            )
            try:
                first_provider_event = await anext(guarded)
            except StopAsyncIteration as exc:
                await guarded.aclose()
                raise ProcessError("provider stream ended without a result") from exc
            except BaseException:
                await guarded.aclose()
                raise

            async def anthropic_stream():
                block_started = False

                def encode(event: str, payload: object) -> str:
                    data = json.dumps(payload, separators=(",", ":"))
                    return f"event: {event}\ndata: {data}\n\n"

                yield encode(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": message_id,
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                            "model": body.model,
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                    },
                )
                try:
                    async def provider_events():
                        yield first_provider_event
                        async for event in guarded:
                            yield event

                    async for event in provider_events():
                        if event.delta:
                            if not block_started:
                                block_started = True
                                yield encode(
                                    "content_block_start",
                                    {
                                        "type": "content_block_start",
                                        "index": 0,
                                        "content_block": {"type": "text", "text": ""},
                                    },
                                )
                            yield encode(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {
                                        "type": "text_delta",
                                        "text": event.delta,
                                    },
                                },
                            )
                        if event.result is None:
                            continue
                        result = event.result
                        stop_reason = "end_turn"
                        stop_sequence = None
                        if result.tool_calls:
                            tool = result.tool_calls[0]
                            block_started = True
                            yield encode(
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": 0,
                                        "content_block": {
                                            "type": "tool_use",
                                            "id": tool.id,
                                            "name": tool.function.name,
                                            "input": {},
                                        },
                                    },
                                )
                            yield encode(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {
                                        "type": "input_json_delta",
                                        "partial_json": tool.function.arguments,
                                    },
                                },
                            )
                        if result.finish_reason == "length":
                            stop_reason = "max_tokens"
                        elif result.finish_reason == "stop":
                            stop_reason = "stop_sequence"
                            stop_sequence = result.stop_sequence
                        elif result.tool_calls:
                            stop_reason = "tool_use"
                        elif result.text and not block_started:
                            block_started = True
                            yield encode(
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": 0,
                                    "content_block": {"type": "text", "text": ""},
                                },
                            )
                            yield encode(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {
                                        "type": "text_delta",
                                        "text": result.text,
                                    },
                                },
                            )
                        if block_started:
                            yield encode(
                                "content_block_stop",
                                {"type": "content_block_stop", "index": 0},
                            )
                        usage = result.usage
                        yield encode(
                            "message_delta",
                            {
                                "type": "message_delta",
                                "delta": {
                                    "stop_reason": stop_reason,
                                    "stop_sequence": stop_sequence,
                                },
                                "usage": {
                                    "output_tokens": (
                                        usage.completion_tokens if usage else 0
                                    )
                                },
                            },
                        )
                except ProcessError as exc:
                    rate_limited = isinstance(exc, ProviderRateLimitError)
                    message = getattr(exc, "public_message", None) or (
                        "Provider subscription quota is exhausted"
                        if rate_limited
                        else "Provider stream failed"
                    )
                    yield encode(
                        "error",
                        {
                            "type": "error",
                            "error": {
                                "type": (
                                    "rate_limit_error"
                                    if rate_limited
                                    else "api_error"
                                ),
                                "message": message,
                            },
                            "request_id": raw_request.state.request_id,
                        },
                    )
                    return
                finally:
                    await guarded.aclose()
                yield encode("message_stop", {"type": "message_stop"})

            return StreamingResponse(
                anthropic_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    **headers,
                },
                background=BackgroundTask(guarded.aclose),
            )

        result = await await_with_disconnect_guard(
            controlled_completion("claude", chat_request), raw_request
        )
        usage = result.usage
        if result.tool_calls:
            tool = result.tool_calls[0]
            content = [
                {
                    "type": "tool_use",
                    "id": tool.id,
                    "name": tool.function.name,
                    "input": json.loads(tool.function.arguments),
                }
            ]
            stop_reason = (
                "max_tokens"
                if result.finish_reason == "length"
                else "tool_use"
            )
        else:
            content = [{"type": "text", "text": result.text or ""}]
            if result.finish_reason == "length":
                stop_reason = "max_tokens"
            elif result.finish_reason == "stop":
                stop_reason = "stop_sequence"
            else:
                stop_reason = "end_turn"
        headers.update(await current_rate_limit_headers("claude"))
        return JSONResponse(
            content={
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": content,
                "model": result.model,
                "stop_reason": stop_reason,
                "stop_sequence": result.stop_sequence,
                "usage": {
                    "input_tokens": usage.prompt_tokens if usage else 0,
                    "output_tokens": usage.completion_tokens if usage else 0,
                },
            },
            headers=headers,
        )

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIRECTORY / "index.html")

    @application.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    application.mount(
        "/static",
        StaticFiles(directory=STATIC_DIRECTORY),
        name="static",
    )
    return application


app = create_app()
