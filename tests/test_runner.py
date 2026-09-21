import asyncio
import sys
from pathlib import Path

import pytest

from app.runner import (
    ProcessNotFoundError,
    ProcessRunner,
    ProviderAuthenticationError,
    provider_error_from_message,
)


def test_provider_login_error_is_classified() -> None:
    error = provider_error_from_message(
        "Not logged in. Please login", provider="claude"
    )

    assert isinstance(error, ProviderAuthenticationError)
    assert error.provider == "claude"


@pytest.mark.asyncio
async def test_missing_command_has_clear_error(tmp_path: Path) -> None:
    runner = ProcessRunner(timeout_seconds=1, max_output_bytes=100)

    with pytest.raises(ProcessNotFoundError, match="command not found"):
        await runner.run(
            ["kessel-command-that-does-not-exist"],
            "",
            tmp_path,
        )


@pytest.mark.asyncio
async def test_cancelling_run_kills_child_process(tmp_path: Path) -> None:
    marker = tmp_path / "completed.txt"
    script = tmp_path / "slow.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "sys.stdin.read()\n"
        "time.sleep(0.5)\n"
        "pathlib.Path(sys.argv[1]).write_text('completed')\n",
        encoding="utf-8",
    )
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=1000)
    task = asyncio.create_task(
        runner.run([sys.executable, str(script), str(marker)], "input", tmp_path)
    )
    await asyncio.sleep(0.1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.6)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_closing_stream_kills_child_process(tmp_path: Path) -> None:
    marker = tmp_path / "completed.txt"
    script = tmp_path / "slow_stream.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "sys.stdin.read()\n"
        "print('ready', flush=True)\n"
        "time.sleep(0.5)\n"
        "pathlib.Path(sys.argv[1]).write_text('completed')\n",
        encoding="utf-8",
    )
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=1000)
    stream = runner.stream_lines(
        [sys.executable, str(script), str(marker)], "input", tmp_path
    )

    assert await anext(stream) == "ready"
    await stream.aclose()
    await asyncio.sleep(0.6)
    assert not marker.exists()
