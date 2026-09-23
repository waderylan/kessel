import json
import os
import time
from pathlib import Path

from kessel_gateway.run_session import RunSessionStore


def test_run_session_claim_heartbeat_and_release(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path))
    store = RunSessionStore()

    session = store.claim("codex")

    assert session is not None
    assert session.owner_pid == os.getpid()
    assert store.claim("claude") is None
    updated = store.heartbeat(session)
    assert updated.heartbeat >= session.heartbeat
    assert store.active() == updated

    store.release(session.token)
    assert store.active() is None
    assert not store.path.exists()


def test_run_session_replaces_corrupt_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path))
    store = RunSessionStore()
    store.path.write_text("not json", encoding="utf-8")

    session = store.claim("claude")

    assert session is not None
    assert json.loads(store.path.read_text(encoding="utf-8"))["token"] == session.token


def test_run_session_removes_stale_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path))
    store = RunSessionStore()
    store.path.write_text(
        json.dumps(
            {
                "token": "stale",
                "owner_pid": os.getpid(),
                "provider": "codex",
                "heartbeat": time.time() - 60,
            }
        ),
        encoding="utf-8",
    )

    assert store.active() is None
    assert not store.path.exists()
