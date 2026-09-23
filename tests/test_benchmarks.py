from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks import local_overhead
from benchmarks import run as live_benchmark


class StreamingResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"OK"}}]}\n'
        yield b"data: [DONE]\n"


def test_deterministic_summary_uses_nearest_rank_p95() -> None:
    summary = local_overhead._summary([1.0, 2.0, 3.0, 4.0, 5.0])

    assert summary["p95_ms"] == 5.0


def test_live_benchmark_sends_api_key(monkeypatch) -> None:
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(request)
        return StreamingResponse()

    monkeypatch.setattr(live_benchmark.urllib.request, "urlopen", fake_urlopen)

    result = live_benchmark.run_once(
        "http://127.0.0.1:8000", "codex", "fresh", "low", "benchmark-key"
    )

    assert result["provider"] == "codex"
    assert captured[0].get_header("Authorization") == "Bearer benchmark-key"


def test_readme_benchmark_markers_are_updated(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    results = tmp_path / "benchmarks" / "results.md"
    results.parent.mkdir()
    results.write_text("generated\n| replacement |\n", encoding="utf-8")
    readme.write_text(
        "before\n<!-- benchmark-table:start -->\nold\n"
        "<!-- benchmark-table:end -->\nafter\n",
        encoding="utf-8",
    )

    live_benchmark.update_readme_table([], readme)

    assert "| replacement |" in readme.read_text(encoding="utf-8")
    assert "\nold\n" not in readme.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_deterministic_overhead_benchmark_runs() -> None:
    result = await local_overhead.measure(1)

    assert set(result["workloads"]) == {
        "health_asgi",
        "gateway_asgi",
        "process_launch",
    }
    assert all(
        workload["runs"] == 1 for workload in result["workloads"].values()
    )
