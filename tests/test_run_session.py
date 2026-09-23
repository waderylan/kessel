import json
import os
import time
from pathlib import Path

from kessel_gateway import run_session
from kessel_gateway.run_session import RunSession, RunSessionStore


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
                "heartbeat": time.time() - 120,
            }
        ),
        encoding="utf-8",
    )

    assert store.active() is None
    assert not store.path.exists()


def test_claim_records_owner_start_time(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path))
    store = RunSessionStore()

    session = store.claim("codex")

    assert session is not None
    assert session.owner_start_time is not None


def test_session_active_when_pid_alive_and_heartbeat_fresh(monkeypatch) -> None:
    session = RunSession(
        token="t", owner_pid=4242, provider="codex", heartbeat=time.time()
    )
    monkeypatch.setattr(run_session, "_process_is_running", lambda pid: True)

    assert run_session._session_is_active(session) is True


def test_session_inactive_when_pid_not_running(monkeypatch) -> None:
    session = RunSession(
        token="t", owner_pid=4242, provider="codex", heartbeat=time.time()
    )
    monkeypatch.setattr(run_session, "_process_is_running", lambda pid: False)

    assert run_session._session_is_active(session) is False


def test_session_inactive_on_pid_reuse_with_mismatched_start_time(
    monkeypatch,
) -> None:
    session = RunSession(
        token="t",
        owner_pid=4242,
        provider="codex",
        heartbeat=time.time(),
        owner_start_time=100.0,
    )
    monkeypatch.setattr(run_session, "_process_is_running", lambda pid: True)
    monkeypatch.setattr(run_session, "_process_start_time", lambda pid: 9999.0)

    assert run_session._session_is_active(session) is False


def test_session_active_when_start_time_matches_within_tolerance(
    monkeypatch,
) -> None:
    session = RunSession(
        token="t",
        owner_pid=4242,
        provider="codex",
        heartbeat=time.time(),
        owner_start_time=100.0,
    )
    monkeypatch.setattr(run_session, "_process_is_running", lambda pid: True)
    monkeypatch.setattr(run_session, "_process_start_time", lambda pid: 101.0)

    assert run_session._session_is_active(session) is True


def test_session_survives_stale_heartbeat_within_wide_tolerance(
    monkeypatch,
) -> None:
    # A laptop sleeping briefly should not make a live owner look stale.
    session = RunSession(
        token="t",
        owner_pid=4242,
        provider="codex",
        heartbeat=time.time() - 45,
    )
    monkeypatch.setattr(run_session, "_process_is_running", lambda pid: True)

    assert run_session._session_is_active(session) is True


def test_session_inactive_when_heartbeat_exceeds_tolerance(monkeypatch) -> None:
    session = RunSession(
        token="t",
        owner_pid=4242,
        provider="codex",
        heartbeat=time.time() - 61,
    )
    monkeypatch.setattr(run_session, "_process_is_running", lambda pid: True)

    assert run_session._session_is_active(session) is False


def test_session_with_matching_start_time_survives_long_sleep(monkeypatch) -> None:
    session = RunSession(
        token="t",
        owner_pid=4242,
        provider="codex",
        heartbeat=time.time() - 7200,
        owner_start_time=1000.0,
    )
    monkeypatch.setattr(run_session, "_process_is_running", lambda pid: True)
    monkeypatch.setattr(run_session, "_process_start_time", lambda pid: 1000.5)

    assert run_session._session_is_active(session) is True

