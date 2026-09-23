from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

from kessel_gateway.config import Settings
from kessel_gateway.main import create_app
from kessel_gateway.models import (
    ChatCompletionRequest,
    FunctionCall,
    ProviderResult,
    ProviderStreamEvent,
    ToolCall,
)
from kessel_gateway.process_security import child_environment
from kessel_gateway.runner import ProcessExitError, ProcessOutputLimitError, ProcessRunner
from kessel_gateway.service import ServiceManager
from kessel_gateway.user_config import UserConfig


class SecurityRegistry:
    names = ("codex", "claude")

    def __init__(self, *, accepts_model: bool = True, error: Exception | None = None):
        self.model_allowed = accepts_model
        self.error = error

    def get(self, name: str):
        return type("Provider", (), {"command": "provider-secret-path"})()

    async def accepts_model(self, provider: str, model: str) -> bool:
        return self.model_allowed

    async def list_models(self, provider: str) -> list[str]:
        return ["approved-model"]

    async def preflight(self, provider: str):
        return None

    async def rate_limit(self, provider: str):
        return None

    async def complete(
        self, provider: str, request: ChatCompletionRequest
    ) -> ProviderResult:
        if self.error:
            raise self.error
        return ProviderResult(text="ok", model=request.model)

    async def stream(self, provider: str, request: ChatCompletionRequest):
        if self.error:
            raise self.error
        yield ProviderStreamEvent(result=ProviderResult(text="ok", model=request.model))


class ToolRegistry(SecurityRegistry):
    async def stream(self, provider: str, request: ChatCompletionRequest):
        yield ProviderStreamEvent(
            result=ProviderResult(
                text=None,
                model=request.model,
                tool_calls=[
                    ToolCall(
                        id="call_test",
                        function=FunctionCall(
                            name="answer", arguments='{"value":"ok"}'
                        ),
                    )
                ],
            )
        )


class LateStreamErrorRegistry(SecurityRegistry):
    async def stream(self, provider: str, request: ChatCompletionRequest):
        yield ProviderStreamEvent(delta="partial")
        raise ProcessExitError(1, "STREAM_STDERR_SECRET")


class WarmCallTrackingRegistry(SecurityRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.preflight_calls: list[str] = []
        self.rate_limit_calls: list[str] = []

    async def preflight(self, provider: str):
        self.preflight_calls.append(provider)

    async def rate_limit(self, provider: str):
        self.rate_limit_calls.append(provider)


def settings(**updates) -> Settings:
    values = {
        "api_key": "audit-key",
        "cors_origins": (
            "http://127.0.0.1:4880",
            "http://localhost:4880",
        ),
        "request_timeout_seconds": 10,
        "max_concurrent_requests": 1,
        "max_output_bytes": 10_000,
        "codex_command": "codex",
        "claude_command": "claude",
        "enforce_cli_versions": False,
        "max_request_bytes": 512,
    }
    values.update(updates)
    return Settings(**values)


@pytest.mark.asyncio
async def test_host_origin_and_simple_content_type_are_rejected() -> None:
    app = create_app(settings(), registry=SecurityRegistry())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        rebinding = await client.get(
            "/health", headers={"host": "attacker.example:4880"}
        )
        cross_origin = await client.get(
            "/health", headers={"origin": "https://attacker.example"}
        )
        simple_post = await client.post(
            "/v1/codex/chat/completions",
            headers={
                "authorization": "Bearer audit-key",
                "content-type": "text/plain",
            },
            content='{"model":"default","messages":[]}',
        )

    assert rebinding.status_code == 421
    assert cross_origin.status_code == 403
    assert simple_post.status_code == 415


@pytest.mark.asyncio
async def test_declared_and_chunked_oversized_bodies_are_rejected() -> None:
    app = create_app(settings(), registry=SecurityRegistry())

    async def chunks():
        yield b'{"model":"default","messages":[{"role":"user","content":"'
        yield b"x" * 600
        yield b'"}]}'

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        declared = await client.post(
            "/v1/codex/chat/completions",
            headers={"authorization": "Bearer audit-key"},
            json={"model": "default", "messages": [{"role": "user", "content": "x" * 600}]},
        )
        chunked = await client.post(
            "/v1/codex/chat/completions",
            headers={
                "authorization": "Bearer audit-key",
                "content-type": "application/json",
            },
            content=chunks(),
        )

    assert declared.status_code == 413
    assert chunked.status_code == 413


@pytest.mark.asyncio
async def test_authentication_fails_closed_and_rotation_is_immediate(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    config = UserConfig(api_key="old-key")
    config.save()
    app = create_app(
        settings(api_key="old-key", reload_api_key_from_config=True),
        registry=SecurityRegistry(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        old_before = await client.get(
            "/v1/claude/models", headers={"authorization": "Bearer old-key"}
        )
        config.with_rotated_key().save()
        old_after = await client.get(
            "/v1/claude/models", headers={"authorization": "Bearer old-key"}
        )
        new_key = UserConfig.load().api_key
        new_after = await client.get(
            "/v1/claude/models",
            headers={"authorization": f"bearer {new_key}"},
        )

    assert old_before.status_code == 200
    assert old_after.status_code == 401
    assert new_after.status_code == 200

    fail_closed = create_app(
        settings(api_key=None, reload_api_key_from_config=False),
        registry=SecurityRegistry(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fail_closed),
        base_url="http://127.0.0.1:4880",
    ) as client:
        response = await client.get("/v1/claude/models")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_file_backed_api_key_parsing_is_cached_until_rotation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    config = UserConfig(api_key="old-key")
    config.save()
    original_load = UserConfig.load.__func__
    load_calls = 0

    def tracked_load(cls):
        nonlocal load_calls
        load_calls += 1
        return original_load(cls)

    monkeypatch.setattr(UserConfig, "load", classmethod(tracked_load))
    app = create_app(
        settings(api_key="old-key", reload_api_key_from_config=True),
        registry=SecurityRegistry(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        first = await client.get(
            "/v1/claude/models", headers={"authorization": "Bearer old-key"}
        )
        second = await client.get(
            "/v1/claude/models", headers={"authorization": "Bearer old-key"}
        )
        rotated = config.with_rotated_key()
        rotated.save()
        third = await client.get(
            "/v1/claude/models",
            headers={"authorization": f"Bearer {rotated.api_key}"},
        )
        malformed = config.path.with_name(".malformed-config.tmp")
        malformed.write_text("{", encoding="utf-8")
        malformed.replace(config.path)
        invalid = await client.get(
            "/v1/claude/models",
            headers={"authorization": f"Bearer {rotated.api_key}"},
        )

    assert first.status_code == second.status_code == third.status_code == 200
    assert invalid.status_code == 503
    assert load_calls == 3


@pytest.mark.asyncio
async def test_health_provider_resolution_uses_short_ttl_cache(monkeypatch) -> None:
    calls: list[str] = []

    def fake_which(command: str) -> str:
        calls.append(command)
        return f"/resolved/{command}"

    monkeypatch.setattr("kessel_gateway.main.shutil.which", fake_which)
    app = create_app(settings(), registry=SecurityRegistry())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        first = await client.get("/health")
        second = await client.get("/health")
        app.state.provider_availability_cache._expires_at = 0
        third = await client.get("/health")

    assert first.status_code == second.status_code == third.status_code == 200
    assert calls == ["provider-secret-path", "provider-secret-path"] * 2


@pytest.mark.asyncio
async def test_health_requires_at_least_one_available_provider(monkeypatch) -> None:
    monkeypatch.setattr("kessel_gateway.main.shutil.which", lambda command: None)
    app = create_app(settings(), registry=SecurityRegistry())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "kessel",
        "status": "degraded",
        "providers": {
            "codex": {"available": False},
            "claude": {"available": False},
        },
        "message": (
            "No supported provider CLI is installed. Install Codex or Claude "
            "Code, then run `kessel setup`."
        ),
    }


@pytest.mark.asyncio
async def test_health_accepts_one_available_provider(monkeypatch) -> None:
    registry = SecurityRegistry()
    monkeypatch.setattr(
        registry,
        "get",
        lambda name: type("Provider", (), {"command": name})(),
    )
    monkeypatch.setattr(
        "kessel_gateway.main.shutil.which",
        lambda command: f"/resolved/{command}" if command == "claude" else None,
    )
    app = create_app(settings(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "kessel",
        "status": "ok",
        "providers": {
            "codex": {"available": False},
            "claude": {"available": True},
        },
    }


@pytest.mark.asyncio
async def test_health_and_provider_errors_do_not_leak_details() -> None:
    secret = "SECRET_FROM_PROVIDER_STDERR"
    app = create_app(
        settings(),
        registry=SecurityRegistry(error=ProcessExitError(1, secret)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        health = await client.get("/health")
        failure = await client.post(
            "/v1/claude/chat/completions",
            headers={"authorization": "Bearer audit-key"},
            json={"model": "default", "messages": [{"role": "user", "content": "x"}]},
        )

    assert "command" not in health.text
    assert "version" not in health.text
    assert "provider-secret-path" not in health.text
    assert secret not in failure.text
    assert failure.json()["error"]["message"] == "Provider process failed"


@pytest.mark.asyncio
async def test_unapproved_model_is_rejected_before_provider_execution() -> None:
    app = create_app(settings(), registry=SecurityRegistry(accepts_model=False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        response = await client.post(
            "/v1/claude/chat/completions",
            headers={"authorization": "Bearer audit-key"},
            json={"model": "hostile/model", "messages": [{"role": "user", "content": "x"}]},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_model"


@pytest.mark.asyncio
async def test_fresh_codex_does_not_invoke_warm_rate_limit_paths() -> None:
    registry = WarmCallTrackingRegistry()
    app = create_app(settings(), registry=registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        response = await client.post(
            "/v1/codex/chat/completions",
            headers={"authorization": "Bearer audit-key"},
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "x"}],
            },
        )

    assert response.status_code == 200
    assert registry.preflight_calls == []
    assert registry.rate_limit_calls == []


@pytest.mark.asyncio
async def test_child_environment_scrubs_service_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KESSEL_API_KEY", "kessel-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    script = tmp_path / "environment.py"
    script.write_text(
        "import json, os\nprint(json.dumps(dict(os.environ)))\n",
        encoding="utf-8",
    )
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=100_000)
    result = await runner.run([sys.executable, str(script)], "", tmp_path)
    environment = json.loads(result.stdout)

    assert "KESSEL_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert environment["NO_COLOR"] == "1"
    assert "KESSEL_API_KEY" not in child_environment()


@pytest.mark.asyncio
async def test_oversized_ndjson_line_has_bounded_error(tmp_path: Path) -> None:
    script = tmp_path / "oversized.py"
    script.write_text(
        "import sys\nsys.stdout.write('x' * 70000 + '\\n')\nsys.stdout.flush()\n",
        encoding="utf-8",
    )
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=100_000)
    stream = runner.stream_lines([sys.executable, str(script)], "", tmp_path)
    with pytest.raises(ProcessOutputLimitError, match="oversized output line"):
        await anext(stream)


def test_config_is_restrictive_and_atomic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    config = UserConfig(api_key="secret")
    config.save()
    config.with_rotated_key().save()

    assert not list(config.path.parent.glob("*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(config.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(config.path.parent.stat().st_mode) == 0o700


def test_blank_environment_key_does_not_disable_saved_key_reload(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_API_KEY", "")
    monkeypatch.delenv("KESSEL_CORS_ORIGINS", raising=False)
    UserConfig(api_key="saved-key").save()

    loaded = Settings.from_environment()

    assert loaded.api_key == "saved-key"
    assert loaded.reload_api_key_from_config is True
    assert "http://[::1]:4880" in loaded.cors_origins


def test_systemd_escaping_handles_special_path_characters() -> None:
    quoted = ServiceManager._systemd_quote('/tmp/a b/$cash/%instance/"quoted"')
    assert quoted == '"/tmp/a b/$$cash/%%instance/\\"quoted\\""'


@pytest.mark.asyncio
async def test_stream_protocol_fields_and_errors_are_safe() -> None:
    tool_app = create_app(settings(), registry=ToolRegistry())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=tool_app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        anthropic = await client.post(
            "/v1/messages",
            headers={"authorization": "Bearer audit-key"},
            json={
                "model": "default",
                "stream": True,
                "tools": [
                    {
                        "name": "answer",
                        "input_schema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                        },
                    }
                ],
                "tool_choice": {"type": "tool", "name": "answer"},
                "messages": [{"role": "user", "content": "x"}],
            },
        )

    frames = [frame for frame in anthropic.text.split("\n\n") if frame]
    payloads = [
        json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))
        for frame in frames
    ]
    tool_start = next(item for item in payloads if item["type"] == "content_block_start")
    tool_delta = next(item for item in payloads if item["type"] == "content_block_delta")
    assert tool_start["content_block"]["input"] == {}
    assert tool_delta["delta"] == {
        "type": "input_json_delta",
        "partial_json": '{"value":"ok"}',
    }

    error_app = create_app(settings(), registry=LateStreamErrorRegistry())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=error_app),
        base_url="http://127.0.0.1:4880",
    ) as client:
        openai = await client.post(
            "/v1/codex/chat/completions",
            headers={"authorization": "Bearer audit-key"},
            json={
                "model": "default",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "x"}],
            },
        )
        anthropic_error = await client.post(
            "/v1/messages",
            headers={"authorization": "Bearer audit-key"},
            json={
                "model": "default",
                "stream": True,
                "messages": [{"role": "user", "content": "x"}],
            },
        )

    openai_payloads = [
        json.loads(line[6:])
        for line in openai.text.splitlines()
        if line.startswith("data: {")
    ]
    assert all("usage" in item for item in openai_payloads if "choices" in item)
    assert "STREAM_STDERR_SECRET" not in openai.text
    assert "Provider stream failed" in openai.text
    assert "STREAM_STDERR_SECRET" not in anthropic_error.text
    assert "event: error" in anthropic_error.text
    assert "event: message_stop" not in anthropic_error.text
