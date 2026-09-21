"""FastAPI application and OpenAI-compatible HTTP routes."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import secrets
import time
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, settings
from app.models import (
    AnthropicMessagesRequest,
    ChatCompletionRequest,
    ChatCompletionResponse,
    CompletionChoice,
    CompletionMessage,
    ErrorDetail,
    ErrorResponse,
    ProviderResult,
)
from app.output_control import control_output_stream
from app.providers.claude import ClaudeProvider
from app.providers.codex import CodexProvider
from app.providers.registry import ProviderRegistry
from app.runner import (
    ProcessError,
    ProcessExitError,
    ProcessNotFoundError,
    ProcessOutputLimitError,
    ProcessRunner,
    ProcessTimeoutError,
    ProviderBusyError,
    ProviderAuthenticationError,
    ProviderRateLimitError,
)
from app.versioning import verify_cli_versions


STATIC_DIRECTORY = Path(__file__).parent / "static"
REQUEST_LOGGER = logging.getLogger("uvicorn.error")
REQUEST_LOGGER.setLevel(logging.INFO)


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
            app.state.cli_versions = await verify_cli_versions(app_settings)
        yield
        close = getattr(app.state.registry, "close", None)
        if close is not None:
            await close()

    application = FastAPI(
        title="Kessel Local API",
        version="0.1.0",
        description=(
            "Stateless OpenAI-compatible Chat Completions over local Codex and "
            "Claude Code subscriptions."
        ),
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.state.registry = registry or create_registry(app_settings)
    application.state.cli_versions = {}

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

    @application.middleware("http")
    async def request_logging(request: Request, call_next):
        request_id = f"req_local_{uuid.uuid4().hex}"
        request.state.request_id = request_id
        started_at = time.monotonic()
        REQUEST_LOGGER.info(
            json.dumps(
                {
                    "event": "request.started",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                },
                separators=(",", ":"),
            )
        )
        try:
            response = await call_next(request)
        except BaseException:
            REQUEST_LOGGER.exception(
                json.dumps(
                    {
                        "event": "request.failed",
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "duration_ms": round(
                            (time.monotonic() - started_at) * 1000, 1
                        ),
                    },
                    separators=(",", ":"),
                )
            )
            raise
        response.headers["x-request-id"] = request_id
        if request.url.path == "/v1/messages":
            response.headers["request-id"] = request_id
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["cache-control"] = "no-cache"
        original_body = response.body_iterator

        async def logged_body():
            completed = False
            try:
                async for chunk in original_body:
                    yield chunk
                completed = True
            except asyncio.CancelledError:
                REQUEST_LOGGER.info(
                    json.dumps(
                        {
                            "event": "request.cancelled",
                            "request_id": request_id,
                            "method": request.method,
                            "path": request.url.path,
                            "duration_ms": round(
                                (time.monotonic() - started_at) * 1000, 1
                            ),
                        },
                        separators=(",", ":"),
                    )
                )
                raise
            except BaseException:
                REQUEST_LOGGER.exception(
                    json.dumps(
                        {
                            "event": "request.stream_failed",
                            "request_id": request_id,
                            "method": request.method,
                            "path": request.url.path,
                            "duration_ms": round(
                                (time.monotonic() - started_at) * 1000, 1
                            ),
                        },
                        separators=(",", ":"),
                    )
                )
                raise
            finally:
                if completed:
                    REQUEST_LOGGER.info(
                        json.dumps(
                            {
                                "event": "request.completed",
                                "request_id": request_id,
                                "method": request.method,
                                "path": request.url.path,
                                "status": response.status_code,
                                "duration_ms": round(
                                    (time.monotonic() - started_at) * 1000, 1
                                ),
                            },
                            separators=(",", ":"),
                        )
                    )

        response.body_iterator = logged_body()
        return response

    async def require_api_key(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> None:
        expected_key = application.state.settings.api_key
        if not expected_key:
            return
        supplied_key = ""
        if authorization and authorization.startswith("Bearer "):
            supplied_key = authorization.removeprefix("Bearer ")
        if not supplied_key and x_api_key:
            supplied_key = x_api_key
        if not secrets.compare_digest(supplied_key, expected_key):
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
        if is_anthropic(request):
            anthropic_types = {
                400: "invalid_request_error",
                401: "authentication_error",
                403: "permission_error",
                404: "not_found_error",
                413: "request_too_large",
                429: "rate_limit_error",
                503: "overloaded_error",
            }
            content = {
                "type": "error",
                "error": {
                    "type": anthropic_types.get(status_code, "api_error"),
                    "message": message,
                },
                "request_id": request.state.request_id,
            }
        else:
            openai_types = {
                401: "authentication_error",
                403: "permission_error",
                429: "rate_limit_error",
            }
            error_type = openai_types.get(
                status_code,
                "invalid_request_error"
                if 400 <= status_code < 500
                else "server_error",
            )
            content = error_content(message, error_type, code, param)
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

        message = str(exc)
        if isinstance(exc, ProviderAuthenticationError) or process_auth_error:
            auth_provider = (
                exc.provider
                if isinstance(exc, ProviderAuthenticationError)
                else provider_name
            )
            display = "Claude Code" if auth_provider == "claude" else "Codex"
            command = "claude login" if auth_provider == "claude" else "codex login"
            message = f"{display} isn't logged in. Run: {command}"
        elif isinstance(exc, ProviderRateLimitError) and exc.retry_after_seconds:
            reset_at = datetime.fromtimestamp(
                time.time() + exc.retry_after_seconds
            ).astimezone()
            message = (
                f"{message}. Quota resets at "
                f"{reset_at.isoformat(timespec='minutes')}"
            )
        if (
            isinstance(exc, ProcessExitError)
            and exc.stderr
            and not process_auth_error
        ):
            message = f"{message}: {exc.stderr[-1000:]}"
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
    async def health() -> dict[str, object]:
        provider_status = {}
        for name in application.state.registry.names:
            command = application.state.registry.get(name).command
            executable = await asyncio.to_thread(shutil.which, command)
            provider_status[name] = {
                "command": command,
                "available": executable is not None,
                "version": (
                    application.state.cli_versions[name].actual
                    if name in application.state.cli_versions
                    else None
                ),
                "tested_version": (
                    application.state.cli_versions[name].expected
                    if name in application.state.cli_versions
                    else None
                ),
            }
        return {"status": "ok", "providers": provider_status}

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

    async def await_or_disconnect(operation, raw_request: Request):
        operation_task = asyncio.create_task(operation)

        async def watch_disconnect() -> None:
            while not operation_task.done():
                if await raw_request.is_disconnected():
                    return
                await asyncio.sleep(0.05)

        disconnect_task = asyncio.create_task(watch_disconnect())
        try:
            done, _ = await asyncio.wait(
                {operation_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            operation_task.cancel()
            disconnect_task.cancel()
            await asyncio.gather(
                operation_task, disconnect_task, return_exceptions=True
            )
            raise
        if operation_task in done:
            disconnect_task.cancel()
            await asyncio.gather(disconnect_task, return_exceptions=True)
            return await operation_task

        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        raise HTTPException(status_code=499, detail="Client disconnected")

    async def preflight_headers(provider: str) -> dict[str, str]:
        snapshot = await application.state.registry.preflight(provider)
        return snapshot.headers() if snapshot is not None else {}

    async def current_rate_limit_headers(provider: str) -> dict[str, str]:
        snapshot = await application.state.registry.rate_limit(provider)
        return snapshot.headers() if snapshot is not None else {}

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
        validate_chat_surface(body)
        headers = await preflight_headers(provider)
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
            provider_stream = controlled_provider_stream(provider, body)
            try:
                first_provider_event = await await_or_disconnect(
                    anext(provider_stream), raw_request
                )
            except StopAsyncIteration as exc:
                raise ProcessError("provider stream ended without a result") from exc
            except BaseException:
                await provider_stream.aclose()
                raise

            async def event_stream():
                emitted_content = False

                def encode(payload: object) -> str:
                    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"

                def chunk(delta: dict[str, object], finish_reason=None) -> dict:
                    return {
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

                yield encode(chunk({"role": "assistant", "content": ""}))
                try:
                    async def provider_events():
                        yield first_provider_event
                        while True:
                            try:
                                yield await await_or_disconnect(
                                    anext(provider_stream), raw_request
                                )
                            except StopAsyncIteration:
                                return

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
                    yield encode(
                        error_content(
                            str(exc),
                            "rate_limit_error" if rate_limited else "server_error",
                            "rate_limit_exceeded" if rate_limited else "stream_error",
                        )
                    )
                finally:
                    await provider_stream.aclose()
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    **headers,
                },
            )

        result = await await_or_disconnect(
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

        message_id = f"msg_local_{uuid.uuid4().hex}"
        if body.stream:
            provider_stream = controlled_provider_stream("claude", chat_request)
            try:
                first_provider_event = await await_or_disconnect(
                    anext(provider_stream), raw_request
                )
            except StopAsyncIteration as exc:
                raise ProcessError("provider stream ended without a result") from exc
            except BaseException:
                await provider_stream.aclose()
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
                        while True:
                            try:
                                yield await await_or_disconnect(
                                    anext(provider_stream), raw_request
                                )
                            except StopAsyncIteration:
                                return

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
                                        "input": json.loads(tool.function.arguments),
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
                                "message": str(exc),
                            },
                            "request_id": raw_request.state.request_id,
                        },
                    )
                finally:
                    await provider_stream.aclose()
                yield encode("message_stop", {"type": "message_stop"})

            return StreamingResponse(
                anthropic_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    **headers,
                },
            )

        result = await await_or_disconnect(
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
