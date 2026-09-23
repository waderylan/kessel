from __future__ import annotations

import httpx
import pytest

from kessel_gateway.config import Settings
from kessel_gateway.main import create_app
from kessel_gateway.models import (
    ChatCompletionRequest,
    ProviderAccountInfo,
    ProviderResult,
    ProviderStreamEvent,
    TokenUsage,
)
from kessel_gateway.runner import (
    ProviderAuthenticationError,
    ProviderCompatibilityError,
    ProviderRateLimitError,
)


class FakeProvider:
    command = "fake"


class FakeRegistry:
    names = ("codex", "claude")

    def get(self, name: str) -> FakeProvider:
        return FakeProvider()

    async def list_models(self, provider_name: str) -> list[str]:
        return [f"{provider_name}-confirmed-model"]

    async def account_infos(self) -> list[ProviderAccountInfo]:
        return [
            ProviderAccountInfo(
                provider="codex",
                status="authenticated",
                auth_method="chatgpt",
                account_type="chatgpt",
                email="codex@example.com",
                subscription="pro",
            ),
            ProviderAccountInfo(
                provider="claude",
                status="authenticated",
                auth_method="claude.ai",
                account_type="firstParty",
                email="claude@example.com",
                organization="Example Org",
                subscription="max",
            ),
        ]

    async def preflight(self, provider_name: str):
        return None

    async def rate_limit(self, provider_name: str):
        return None

    async def accepts_model(self, provider_name: str, model: str) -> bool:
        return True

    async def complete(
        self, provider_name: str, request: ChatCompletionRequest
    ) -> ProviderResult:
        return ProviderResult(
            text=f"{provider_name}: {request.messages[-1].text()}",
            model=request.model,
            usage=TokenUsage(
                prompt_tokens=4,
                completion_tokens=2,
                total_tokens=6,
            ),
        )

    async def stream(self, provider_name: str, request: ChatCompletionRequest):
        yield ProviderStreamEvent(delta=f"{provider_name}: ")
        yield ProviderStreamEvent(
            delta=request.messages[-1].text(),
            result=ProviderResult(
                text=f"{provider_name}: {request.messages[-1].text()}",
                model=request.model,
                usage=TokenUsage(
                    prompt_tokens=4,
                    completion_tokens=2,
                    total_tokens=6,
                ),
            ),
        )


def make_settings(api_key: str | None = None) -> Settings:
    return Settings(
        api_key=api_key,
        cors_origins=(),
        request_timeout_seconds=10,
        max_concurrent_requests=1,
        max_output_bytes=10_000,
        codex_command="codex",
        claude_command="claude",
        allow_unauthenticated=api_key is None,
    )


@pytest.mark.asyncio
async def test_chat_completion_uses_openai_shape() -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            "/v1/codex/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"] == {
        "role": "assistant",
        "content": "codex: Hello",
        "refusal": None,
        "tool_calls": None,
    }
    assert payload["usage"]["total_tokens"] == 6
    assert response.headers["x-request-id"].startswith("req_local_")


@pytest.mark.asyncio
async def test_reasoning_effort_defaults_to_low() -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            "/v1/codex/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_streaming_uses_openai_sse_shape() -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            "/v1/claude/chat/completions",
            json={
                "model": "default",
                "stream": True,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"object":"chat.completion.chunk"' in response.text
    assert '"content":"claude: "' in response.text
    assert response.text.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_configured_api_key_is_required() -> None:
    app = create_app(make_settings(api_key="secret"), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        unauthorized = await client.get("/v1/codex/models")
        authorized = await client.get(
            "/v1/codex/models",
            headers={"Authorization": "Bearer secret"},
        )

    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["type"] == "authentication_error"
    assert unauthorized.json()["error"]["code"] == "invalid_api_key"
    assert "Authorization: Bearer" in unauthorized.json()["error"]["message"]
    assert "X-API-Key" in unauthorized.json()["error"]["message"]
    assert "Run kessel key to see yours" in unauthorized.json()["error"]["message"]
    assert authorized.status_code == 200


@pytest.mark.asyncio
async def test_provider_accounts_are_normalized_and_authenticated() -> None:
    app = create_app(make_settings(api_key="secret"), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:4880"
    ) as client:
        unauthorized = await client.get("/v1/providers/accounts")
        response = await client.get(
            "/v1/providers/accounts",
            headers={"Authorization": "Bearer secret"},
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "object": "list",
        "data": [
            {
                "provider": "codex",
                "status": "authenticated",
                "auth_method": "chatgpt",
                "account_type": "chatgpt",
                "email": "codex@example.com",
                "organization": None,
                "subscription": "pro",
            },
            {
                "provider": "claude",
                "status": "authenticated",
                "auth_method": "claude.ai",
                "account_type": "firstParty",
                "email": "claude@example.com",
                "organization": "Example Org",
                "subscription": "max",
            },
        ],
    }


@pytest.mark.asyncio
async def test_anthropic_auth_error_uses_anthropic_shape() -> None:
    app = create_app(make_settings(api_key="secret"), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            "/v1/messages",
            headers={"x-api-key": "wrong"},
            json={"model": "default", "messages": [{"role": "user", "content": "x"}]},
        )

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_unknown_provider_returns_404() -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            "/v1/unknown/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_model_name_rejects_shell_metacharacters() -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            "/v1/codex/chat/completions",
            json={
                "model": "default&whoami",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "model"


@pytest.mark.asyncio
async def test_anthropic_messages_shape() -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    assert payload["content"] == [{"type": "text", "text": "claude: Hello"}]
    assert payload["stop_reason"] == "end_turn"
    assert payload.get("request_id") is None
    assert response.headers["request-id"].startswith("req_local_")


@pytest.mark.asyncio
async def test_warm_claude_is_rejected_to_preserve_statelessness() -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            "/v1/claude/chat/completions",
            json={
                "model": "default",
                "backend": "warm",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 400
    assert "only available for Codex" in response.json()["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("n", 2), ("parallel_tool_calls", True)],
)
async def test_unsupported_openai_controls_return_clear_400(
    field: str, value: object
) -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            "/v1/codex/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Hello"}],
                field: value,
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == field
    assert response.json()["error"]["code"] == "unsupported_parameter"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, "1", 1.0])
async def test_n_requires_a_strict_json_integer(value: object) -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post(
            "/v1/codex/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Hello"}],
                "n": value,
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "n"


@pytest.mark.asyncio
async def test_anthropic_named_tool_must_match_supplied_tool() -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Weather"}],
                "tools": [
                    {"name": "weather", "input_schema": {"type": "object"}}
                ],
                "tool_choice": {"type": "tool", "name": "missing"},
            },
        )

    assert response.status_code == 400
    assert "must match the supplied tool" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_anthropic_errors_use_anthropic_shape() -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": 0,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 400
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["request_id"] == response.headers["request-id"]


@pytest.mark.asyncio
async def test_models_are_discovered_from_provider() -> None:
    app = create_app(make_settings(), registry=FakeRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.get("/v1/codex/models")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [
        "codex-confirmed-model"
    ]
    assert response.headers["x-kessel-model-discovery"] == "provider"


class RateLimitedRegistry(FakeRegistry):
    async def preflight(self, provider_name: str):
        raise ProviderRateLimitError("quota exhausted", retry_after_seconds=45)


class LoggedOutRegistry(FakeRegistry):
    async def preflight(self, provider_name: str):
        raise ProviderAuthenticationError(provider_name, "not logged in")


class IncompatibleRegistry(FakeRegistry):
    async def accepts_model(self, provider_name: str, model: str) -> bool:
        raise ProviderCompatibilityError(
            provider_name, "missing required capabilities: --ephemeral"
        )


@pytest.mark.asyncio
async def test_incompatible_provider_returns_repairable_error() -> None:
    app = create_app(make_settings(), registry=IncompatibleRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:4880"
    ) as client:
        response = await client.post(
            "/v1/codex/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "x"}],
            },
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_incompatible"
    message = response.json()["error"]["message"]
    assert "missing required capabilities: --ephemeral" in message
    assert "Known stable Codex version: 0.155.1" in message
    assert "npm install -g @openai/codex@0.155.1" in message


@pytest.mark.asyncio
async def test_logged_out_provider_has_exact_repair_command() -> None:
    app = create_app(make_settings(), registry=LoggedOutRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(
            "/v1/claude/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "x"}],
            },
        )
        anthropic_response = await client.post(
            "/v1/messages",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "x"}],
            },
        )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "server_error"
    assert response.json()["error"]["message"] == (
        "Claude Code isn't logged in. Run: claude login"
    )
    assert anthropic_response.status_code == 503
    assert anthropic_response.json()["error"]["type"] == "api_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload", "anthropic"),
    [
            (
                "/v1/codex/chat/completions",
                {
                    "model": "default",
                    "backend": "warm",
                    "messages": [{"role": "user", "content": "x"}],
                },
            False,
        ),
        (
            "/v1/messages",
            {"model": "default", "messages": [{"role": "user", "content": "x"}]},
            True,
        ),
    ],
)
async def test_rate_limit_is_a_real_429(
    path: str, payload: dict, anthropic: bool
) -> None:
    app = create_app(make_settings(), registry=RateLimitedRegistry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4880") as client:
        response = await client.post(path, json=payload)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "45"
    assert response.headers["ratelimit-remaining"] == "0"
    error = response.json()
    assert "Quota resets at" in error["error"]["message"]
    if anthropic:
        assert error["error"]["type"] == "rate_limit_error"
    else:
        assert error["error"]["code"] == "rate_limit_exceeded"
