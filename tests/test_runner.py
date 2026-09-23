import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kessel_gateway.runner import (
    ProcessExitError,
    ProcessNotFoundError,
    ProcessRunner,
    ProviderAuthenticationError,
    hidden_process_options,
    provider_process_options,
    provider_error_from_message,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows process flags")
def test_provider_processes_never_open_console_windows() -> None:
    provider_flags = int(provider_process_options()["creationflags"])
    helper_flags = int(hidden_process_options()["creationflags"])

    assert provider_flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert provider_flags & subprocess.CREATE_NO_WINDOW
    assert helper_flags & subprocess.CREATE_NO_WINDOW


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
async def test_nonzero_process_error_retains_stdout(tmp_path: Path) -> None:
    script = tmp_path / "exit.py"
    script.write_text(
        "import sys\nprint('structured output')\n"
        "print('failure', file=sys.stderr)\nsys.exit(3)\n",
        encoding="utf-8",
    )
    runner = ProcessRunner(timeout_seconds=2, max_output_bytes=1000)

    with pytest.raises(ProcessExitError) as error:
        await runner.run([sys.executable, str(script)], "", tmp_path)

    assert error.value.return_code == 3
    assert error.value.stdout.strip() == "structured output"
    assert error.value.stderr == "failure"


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


@pytest.mark.asyncio
async def test_large_stderr_does_not_count_toward_output_limit(tmp_path: Path) -> None:
    script = tmp_path / "chatty_stderr.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('e' * 200_000)\n"
        "sys.stderr.flush()\n"
        "print('ok')\n",
        encoding="utf-8",
    )
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=1_000)

    result = await runner.run([sys.executable, str(script)], "", tmp_path)

    assert result.stdout.strip() == "ok"


@pytest.mark.asyncio
async def test_large_stderr_does_not_count_toward_stream_output_limit(
    tmp_path: Path,
) -> None:
    script = tmp_path / "chatty_stderr_stream.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('e' * 200_000)\n"
        "sys.stderr.flush()\n"
        "print('ok')\n",
        encoding="utf-8",
    )
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=1_000)
    stream = runner.stream_lines([sys.executable, str(script)], "", tmp_path)

    assert await anext(stream) == "ok"


@pytest.mark.asyncio
async def test_stream_preserves_utf8_split_across_pipe_reads(tmp_path: Path) -> None:
    script = tmp_path / "split_utf8.py"
    script.write_text(
        "import sys, time\n"
        "payload = 'snowman ☃'.encode('utf-8')\n"
        "sys.stdout.buffer.write(payload[:-1]); sys.stdout.buffer.flush()\n"
        "time.sleep(0.05)\n"
        "sys.stdout.buffer.write(payload[-1:] + b'\\n'); sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    runner = ProcessRunner(timeout_seconds=5, max_output_bytes=1000)
    stream = runner.stream_lines([sys.executable, str(script)], "", tmp_path)

    assert await anext(stream) == "snowman ☃"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_stream_framing_volume_does_not_hit_output_limit(tmp_path: Path) -> None:
    # Claude's stream-json framing is many times larger than the reply text;
    # providers cap the text, so the line stream itself must not.
    script = tmp_path / "chatty_stream.py"
    script.write_text(
        "for i in range(3000):\n"
        "    print('{\"type\":\"stream_event\",\"pad\":\"' + 'x' * 80 + '\"}')\n",
        encoding="utf-8",
    )
    runner = ProcessRunner(timeout_seconds=10, max_output_bytes=1_000)
    stream = runner.stream_lines([sys.executable, str(script)], "", tmp_path)

    lines = [line async for line in stream]

    assert len(lines) == 3000
