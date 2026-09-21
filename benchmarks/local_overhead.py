"""Measure deterministic Python gateway, health, and process-launch overhead."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.config import Settings
from app.main import create_app
from app.models import ChatCompletionRequest, ProviderResult, ProviderStreamEvent
from app.runner import ProcessRunner


class _Provider:
    command = sys.executable


class DeterministicRegistry:
    names = ("codex", "claude")

    def get(self, provider: str) -> _Provider:
        return _Provider()

    async def accepts_model(self, provider: str, model: str) -> bool:
        return True

    async def list_models(self, provider: str) -> list[str]:
        return ["default"]

    async def preflight(self, provider: str):
        return None

    async def rate_limit(self, provider: str):
        return None

    async def complete(
        self, provider: str, request: ChatCompletionRequest
    ) -> ProviderResult:
        return ProviderResult(text="BENCHMARK_OK", model=request.model)

    async def stream(self, provider: str, request: ChatCompletionRequest):
        yield ProviderStreamEvent(
            result=ProviderResult(text="BENCHMARK_OK", model=request.model)
        )

    async def close(self) -> None:
        return None


def _summary(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    return {
        "runs": len(samples),
        "mean_ms": round(statistics.mean(samples), 3),
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(
            ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 3
        ),
    }


async def measure(runs: int) -> dict[str, object]:
    settings = Settings(
        api_key=None,
        cors_origins=(),
        request_timeout_seconds=10,
        max_concurrent_requests=2,
        max_output_bytes=100_000,
        codex_command=sys.executable,
        claude_command=sys.executable,
        enforce_cli_versions=False,
        allow_unauthenticated=True,
    )
    app = create_app(settings, registry=DeterministicRegistry())
    transport = httpx.ASGITransport(app=app)
    gateway_samples: list[float] = []
    health_samples: list[float] = []
    payload = {
        "model": "default",
        "messages": [{"role": "user", "content": "benchmark"}],
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8000"
    ) as client:
        await client.get("/health")
        await client.post("/v1/codex/chat/completions", json=payload)
        for _ in range(runs):
            started = time.perf_counter()
            response = await client.get("/health")
            response.raise_for_status()
            health_samples.append((time.perf_counter() - started) * 1000)

            started = time.perf_counter()
            response = await client.post(
                "/v1/codex/chat/completions", json=payload
            )
            response.raise_for_status()
            if response.json()["choices"][0]["message"]["content"] != "BENCHMARK_OK":
                raise RuntimeError("unexpected deterministic gateway response")
            gateway_samples.append((time.perf_counter() - started) * 1000)

    runner = ProcessRunner(timeout_seconds=10, max_output_bytes=100_000)
    process_samples: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        result = await runner.run(
            [sys.executable, "-c", "print('BENCHMARK_OK')"],
            "",
            Path.cwd(),
        )
        if result.stdout.strip() != "BENCHMARK_OK":
            raise RuntimeError("unexpected deterministic process response")
        process_samples.append((time.perf_counter() - started) * 1000)

    return {
        "workloads": {
            "health_asgi": _summary(health_samples),
            "gateway_asgi": _summary(gateway_samples),
            "process_launch": _summary(process_samples),
        }
    }


def write_report(result: dict[str, object], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "overhead-results.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    workloads = result["workloads"]
    lines = [
        "# Deterministic local-overhead benchmark",
        "",
        "| Workload | Runs | Mean | p50 | p95 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, values in workloads.items():
        lines.append(
            f"| {name} | {values['runs']} | {values['mean_ms']:.3f} ms | "
            f"{values['p50_ms']:.3f} ms | {values['p95_ms']:.3f} ms |"
        )
    (output / "overhead-results.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    result = asyncio.run(measure(args.runs))
    write_report(result, args.output_dir)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
