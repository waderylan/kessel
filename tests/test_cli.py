import asyncio
import os
from pathlib import Path

import pytest

from kessel_gateway import cli
from kessel_gateway import __version__
from kessel_gateway.models import ProviderAccountInfo
from kessel_gateway.providers.health import ProviderHealth
from kessel_gateway.run_session import RunSession
from kessel_gateway.service import ServiceManager
from kessel_gateway.user_config import UserConfig


@pytest.fixture
def configured(tmp_path: Path, monkeypatch) -> UserConfig:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    config = UserConfig(api_key="kessel_test_real_key")
    config.save()
    return config


@pytest.fixture(autouse=True)
def _stub_tokenizer_warmup(monkeypatch):
    # `kessel setup` pre-warms the tokenizer, which otherwise downloads data
    # over the network. Stub it so the CLI test suite stays fast and offline.
    monkeypatch.setattr(
        "kessel_gateway.output_control.estimate_tokens", lambda text: 1
    )


def test_cli_version(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"kessel {__version__}"


@pytest.mark.parametrize(
    ("shell", "expected_template"),
    [
        ("posix", "export OPENAI_BASE_URL={base_url}/v1/claude"),
        ("fish", "set -gx OPENAI_BASE_URL '{base_url}/v1/claude';"),
        (
            "powershell",
            "$env:OPENAI_BASE_URL = '{base_url}/v1/claude'",
        ),
    ],
)
def test_env_output_for_each_shell(
    configured: UserConfig, shell: str, expected_template: str
) -> None:
    output = cli.render_env(configured, "claude", shell)

    assert expected_template.format(base_url=configured.base_url) in output
    assert "kessel_test_real_key" in output
    assert "ANTHROPIC_BASE_URL" in output


def test_env_provider_selects_openai_route(configured: UserConfig) -> None:
    output = cli.render_env(configured, "codex", "posix")

    assert f"OPENAI_BASE_URL={configured.base_url}/v1/codex" in output
    assert f"ANTHROPIC_BASE_URL={configured.base_url}" in output


@pytest.mark.parametrize("target", cli.CONNECT_TARGETS)
def test_each_connect_target_renders_real_key_and_url(
    configured: UserConfig, target: str
) -> None:
    output = cli.render_connect(configured, "claude", target)

    assert "kessel_test_real_key" in output
    assert configured.base_url in output
    assert "Paste" in output


def test_unknown_connect_target_gets_generic_pair(configured: UserConfig) -> None:
    output = cli.render_connect(configured, "claude", "my-tool")

    assert (
        f"OpenAI-compatible base URL: {configured.base_url}/v1/claude" in output
    )
    assert "API key: kessel_test_real_key" in output


def test_connect_provider_selects_openai_route(configured: UserConfig) -> None:
    output = cli.render_connect(configured, "codex", "curl")

    assert f"{configured.base_url}/v1/codex" in output


def test_connect_codex_notes_anthropic_routes_use_claude(
    configured: UserConfig,
) -> None:
    output = cli.render_connect(configured, "codex", "anthropic-python")

    assert "Anthropic SDK routes always use Claude Code" in output


def test_connect_claude_has_no_anthropic_note(configured: UserConfig) -> None:
    output = cli.render_connect(configured, "claude", "anthropic-python")

    assert "Anthropic SDK routes always use Claude Code" not in output


def test_doctor_reports_incompatible_provider(monkeypatch, capsys) -> None:
    check = ProviderHealth(
        "codex",
        "Codex",
        installed=True,
        authenticated=False,
        version="0.200.0",
        detail="missing required capabilities: --ephemeral",
        executable="codex",
        compatible=False,
    )
    monkeypatch.setattr(cli, "check_providers", lambda: [check])

    assert cli.main(["doctor"]) == 1

    output = capsys.readouterr().out
    assert "Codex is incompatible (0.200.0)" in output
    assert "missing required capabilities: --ephemeral" in output
    assert "Known stable version: 0.155.1" in output
    assert "npm install -g @openai/codex@0.155.1" in output


@pytest.mark.parametrize("missing_count", [0, 1, 2])
def test_accounts_handles_every_provider_availability_combination(
    missing_count: int, monkeypatch, capsys
) -> None:
    names = (("claude", "Claude Code"), ("codex", "Codex"))
    checks = [
        ProviderHealth(
            name,
            display,
            installed=index >= missing_count,
            authenticated=index >= missing_count,
            executable=name if index >= missing_count else None,
        )
        for index, (name, display) in enumerate(names)
    ]
    accounts = [
        ProviderAccountInfo(
            provider=check.name,
            status=("authenticated" if check.installed else "not_installed"),
            email=(f"{check.name}@example.com" if check.installed else None),
            subscription=("pro" if check.installed else None),
        )
        for check in checks
    ]

    async def fake_read_provider_accounts(received):
        assert received == checks
        return accounts

    monkeypatch.setattr(cli, "check_providers", lambda: checks)
    monkeypatch.setattr(
        cli, "read_provider_accounts", fake_read_provider_accounts
    )

    assert cli.main(["accounts"]) == 0

    output = capsys.readouterr().out
    assert "Claude Code account:" in output
    assert "Codex account:" in output
    assert output.count("[missing]") == missing_count


@pytest.mark.asyncio
async def test_setup_displays_provider_accounts(
    monkeypatch, capsys
) -> None:
    check = ProviderHealth("codex", "Codex", True, True, "1.0.0")

    async def fake_read_provider_accounts(received):
        assert received == [check]
        return [
            ProviderAccountInfo(
                provider="codex",
                status="authenticated",
                email="codex@example.com",
                subscription="pro",
                auth_method="chatgpt",
            )
        ]

    async def fake_acquire_runtime(config, provider):
        assert provider == "codex"
        return "existing", None, None

    monkeypatch.setattr(
        cli, "read_provider_accounts", fake_read_provider_accounts
    )
    monkeypatch.setattr(cli, "_acquire_runtime", fake_acquire_runtime)
    monkeypatch.setattr(
        cli, "_test_provider", lambda config, provider: (True, "OK")
    )

    failed = await cli._setup_provider_tests(
        UserConfig(api_key="kessel_test_key"), [check]
    )

    output = capsys.readouterr().out
    assert failed is False
    assert "Provider accounts" in output
    assert "Codex account: codex@example.com | plan=pro | auth=chatgpt" in output


def test_setup_is_idempotent(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    check = ProviderHealth("claude", "Claude Code", True, True, "1.0.0")
    monkeypatch.setattr(cli, "check_providers", lambda: [check])
    monkeypatch.setattr(cli, "_test_provider", lambda config, provider: (True, "OK"))
    monkeypatch.setattr(
        cli,
        "_copy_to_clipboard",
        lambda value: pytest.fail("setup must not copy secrets to the clipboard"),
    )

    test_calls: list[list[ProviderHealth]] = []

    async def fake_setup_tests(
        config: UserConfig, working: list[ProviderHealth]
    ) -> bool:
        test_calls.append(working)
        return False

    monkeypatch.setattr(cli, "_setup_provider_tests", fake_setup_tests)

    assert cli.main(["setup"]) == 0
    first_key = UserConfig.load().api_key
    first_output = capsys.readouterr().out
    assert "Generated a local API key" in first_output
    assert first_key not in first_output
    assert "run `kessel key` to reveal it" in first_output
    assert "kessel run --provider claude -- python your_app.py" in first_output
    assert "does not leave Kessel running" in first_output

    assert cli.main(["setup"]) == 0
    second_output = capsys.readouterr().out
    assert "API key already exists" in second_output
    assert UserConfig.load().api_key == first_key
    assert len(test_calls) == 2


def test_setup_succeeds_with_one_provider_and_configures_only_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    checks = [
        ProviderHealth("codex", "Codex", False, False),
        ProviderHealth(
            "claude",
            "Claude Code",
            True,
            True,
            "1.0.0",
            executable="resolved-claude",
        ),
    ]
    monkeypatch.setattr(cli, "check_providers", lambda: checks)
    tested: list[str] = []

    async def fake_setup_tests(
        config: UserConfig, working: list[ProviderHealth]
    ) -> bool:
        tested.extend(check.name for check in working)
        return False

    monkeypatch.setattr(cli, "_setup_provider_tests", fake_setup_tests)

    assert cli.main(["setup"]) == 0

    config = UserConfig.load()
    output = capsys.readouterr().out
    assert config.claude_command == "resolved-claude"
    assert config.codex_command is None
    assert tested == ["claude"]
    assert "Claude Code is ready" in output
    assert "the other provider is optional" in output
    assert "OpenAI base URL (Claude)" in output
    assert "OpenAI base URL (Codex)" not in output
    assert "kessel run --provider claude" in output


def test_setup_succeeds_with_only_codex(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    checks = [
        ProviderHealth(
            "codex",
            "Codex",
            True,
            True,
            "1.0.0",
            executable="resolved-codex",
        ),
        ProviderHealth("claude", "Claude Code", False, False),
    ]
    monkeypatch.setattr(cli, "check_providers", lambda: checks)

    async def fake_setup_tests(
        config: UserConfig, working: list[ProviderHealth]
    ) -> bool:
        assert [check.name for check in working] == ["codex"]
        return False

    monkeypatch.setattr(cli, "_setup_provider_tests", fake_setup_tests)

    assert cli.main(["setup"]) == 0

    config = UserConfig.load()
    output = capsys.readouterr().out
    assert config.codex_command == "resolved-codex"
    assert config.claude_command is None
    assert "Codex is ready" in output
    assert "OpenAI base URL (Codex)" in output
    assert "OpenAI base URL (Claude)" not in output
    assert "Anthropic base URL" not in output
    assert "kessel run --provider codex" in output


def test_setup_fails_without_a_working_provider_and_does_not_create_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    checks = [
        ProviderHealth("codex", "Codex", False, False),
        ProviderHealth("claude", "Claude Code", True, False),
    ]
    monkeypatch.setattr(cli, "check_providers", lambda: checks)
    monkeypatch.setattr(
        cli,
        "_setup_provider_tests",
        lambda *args: pytest.fail("provider tests must not run"),
    )

    assert cli.main(["setup"]) == 1

    captured = capsys.readouterr()
    assert not UserConfig().path.exists()
    assert "Codex is not installed" in captured.out
    assert "Claude Code isn't logged in" in captured.out
    assert "needs at least one installed and logged-in provider" in captured.err
    assert "run `kessel setup` again" in captured.err


def test_run_before_setup_fails_cleanly_without_creating_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))

    assert cli.main(["run", "--provider", "codex"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Kessel is not set up. Run: kessel setup\n"
    assert not UserConfig().path.exists()


def test_client_environment_injects_kessel_values(
    configured: UserConfig, monkeypatch
) -> None:
    monkeypatch.setenv("KEEP_ME", "yes")

    environment = cli.client_environment(configured, "codex")

    assert environment["KEEP_ME"] == "yes"
    assert environment["OPENAI_BASE_URL"].endswith("/v1/codex")
    assert environment["OPENAI_API_KEY"] == "kessel_test_real_key"
    assert environment["ANTHROPIC_BASE_URL"] == configured.base_url
    assert environment["ANTHROPIC_API_KEY"] == "kessel_test_real_key"


def test_run_reports_durable_kessel(
    configured: UserConfig, monkeypatch, capsys
) -> None:
    async def fake_acquire(config: UserConfig, provider: str):
        return "durable", None, None

    monkeypatch.setattr(cli, "_acquire_runtime", fake_acquire)

    assert cli.main(["run", "--provider", "codex"]) == 0
    output = capsys.readouterr().out
    assert "Durable Kessel detected" in output
    assert "No application command was supplied" in output


def test_run_passes_application_as_an_argument_array(
    configured: UserConfig, monkeypatch
) -> None:
    received: list[str] = []

    async def fake_acquire(config: UserConfig, provider: str):
        return "durable", None, None

    async def fake_run_application(
        config: UserConfig,
        provider: str,
        command: list[str],
        expected_session,
        shutdown,
    ) -> int:
        received.extend(command)
        return 7

    monkeypatch.setattr(cli, "_acquire_runtime", fake_acquire)
    monkeypatch.setattr(cli, "_run_application", fake_run_application)

    assert (
        cli.main(
            ["run", "--provider", "codex", "--", "python", "app.py", "a b"]
        )
        == 7
    )
    assert received == ["python", "app.py", "a b"]


def test_key_rotation_does_not_print_secret(
    configured: UserConfig, capsys
) -> None:
    old_key = configured.api_key

    assert cli.main(["key", "--rotate"]) == 0
    output = capsys.readouterr().out
    new_key = UserConfig.load().api_key

    assert new_key != old_key
    assert old_key not in output
    assert new_key not in output
    assert "rotated" in output


def test_status_has_actionable_not_running_error(
    configured: UserConfig, monkeypatch, capsys
) -> None:
    class StoppedServiceManager:
        def __init__(self, config: UserConfig) -> None:
            pass

        def health(self) -> dict | None:
            return None

    monkeypatch.setattr(cli, "ServiceManager", StoppedServiceManager)

    assert cli.main(["status"]) == 1
    assert (
        capsys.readouterr().err
        == "Kessel isn't running. Use: kessel run --provider codex or kessel start\n"
    )


def test_status_reports_degraded(configured: UserConfig, monkeypatch, capsys) -> None:
    class DegradedServiceManager:
        def __init__(self, config: UserConfig) -> None:
            pass

        def health(self) -> dict:
            return {
                "service": "kessel",
                "status": "degraded",
                "providers": {
                    "codex": {"available": False},
                    "claude": {"available": True},
                },
            }

    monkeypatch.setattr(cli, "ServiceManager", DegradedServiceManager)

    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "is running" in output
    assert "degraded: codex unavailable" in output


def test_status_running_without_degraded_suffix(
    configured: UserConfig, monkeypatch, capsys
) -> None:
    class OkServiceManager:
        def __init__(self, config: UserConfig) -> None:
            pass

        def health(self) -> dict:
            return {"service": "kessel", "status": "ok", "providers": {}}

    monkeypatch.setattr(cli, "ServiceManager", OkServiceManager)

    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "degraded" not in output


def test_render_env_uses_environment_key_override(
    configured: UserConfig, monkeypatch
) -> None:
    monkeypatch.setenv("KESSEL_API_KEY", "kessel_env_override")

    output = cli.render_env(configured, "claude", "posix")

    assert "kessel_env_override" in output
    assert "kessel_test_real_key" not in output


def test_default_provider_prefers_sole_configured_command() -> None:
    codex_only = UserConfig(api_key="k", codex_command="codex")
    claude_only = UserConfig(api_key="k", claude_command="claude")
    neither = UserConfig(api_key="k")
    both = UserConfig(api_key="k", codex_command="codex", claude_command="claude")

    assert cli._default_provider(codex_only) == "codex"
    assert cli._default_provider(claude_only) == "claude"
    assert cli._default_provider(neither) == "claude"
    assert cli._default_provider(both) == "claude"


def test_env_falls_back_to_default_provider(
    configured: UserConfig, monkeypatch
) -> None:
    codex_config = UserConfig(
        api_key=configured.api_key, port=configured.port, codex_command="codex"
    )
    codex_config.save()
    seen_providers: list[str] = []
    monkeypatch.setattr(
        cli,
        "render_env",
        lambda cfg, provider, shell: seen_providers.append(provider) or "ok",
    )

    assert cli.main(["env"]) == 0
    assert seen_providers == ["codex"]


def test_setup_port_flag_saves_port(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    check = ProviderHealth("claude", "Claude Code", True, True, "1.0.0")
    monkeypatch.setattr(cli, "check_providers", lambda: [check])

    async def fake_setup_tests(config: UserConfig, working) -> bool:
        return False

    monkeypatch.setattr(cli, "_setup_provider_tests", fake_setup_tests)

    assert cli.main(["setup", "--port", "9001"]) == 0
    assert UserConfig.load().port == 9001


def test_setup_rejects_invalid_port(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))

    assert cli.main(["setup", "--port", "70000"]) == 1
    assert "port must be between 1 and 65535" in capsys.readouterr().err
    assert not UserConfig().path.exists()


def test_setup_reports_token_counter_ready(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    check = ProviderHealth("claude", "Claude Code", True, True, "1.0.0")
    monkeypatch.setattr(cli, "check_providers", lambda: [check])

    async def fake_setup_tests(config: UserConfig, working) -> bool:
        return False

    monkeypatch.setattr(cli, "_setup_provider_tests", fake_setup_tests)

    assert cli.main(["setup"]) == 0
    assert "[ok] Token counter ready" in capsys.readouterr().out


def test_setup_reports_token_counter_warning_when_offline(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    check = ProviderHealth("claude", "Claude Code", True, True, "1.0.0")
    monkeypatch.setattr(cli, "check_providers", lambda: [check])

    async def fake_setup_tests(config: UserConfig, working) -> bool:
        return False

    monkeypatch.setattr(cli, "_setup_provider_tests", fake_setup_tests)

    def boom(text: str) -> int:
        raise RuntimeError("offline")

    monkeypatch.setattr("kessel_gateway.output_control.estimate_tokens", boom)

    assert cli.main(["setup"]) == 0
    output = capsys.readouterr().out
    assert "[warn] Token counter data could not be downloaded" in output


def test_logs_prints_tail(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    log_dir = tmp_path / "state" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "kessel.log").write_text(
        "\n".join(f"line {i}" for i in range(30)) + "\n", encoding="utf-8"
    )

    assert cli.main(["logs", "-n", "5"]) == 0
    output = capsys.readouterr().out.strip().splitlines()
    assert output == [f"line {i}" for i in range(25, 30)]


def test_logs_missing_file_is_an_actionable_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))

    assert cli.main(["logs"]) == 1
    assert "No log file yet" in capsys.readouterr().err


def test_uninstall_service_stops_and_removes_registration(
    configured: UserConfig, monkeypatch, capsys
) -> None:
    calls: list[str] = []

    class FakeManager:
        def __init__(self, config: UserConfig) -> None:
            self.log_directory = Path("state") / "logs"

        def uninstall(self) -> None:
            calls.append("uninstall")

    monkeypatch.setattr(cli, "ServiceManager", FakeManager)

    assert cli.main(["uninstall-service"]) == 0
    assert calls == ["uninstall"]
    output = capsys.readouterr().out
    assert "Configuration and API key remain" in output
    assert "Logs remain" in output


@pytest.mark.asyncio
async def test_owned_server_heartbeat_failure_triggers_shutdown(
    monkeypatch, capsys
) -> None:
    config = UserConfig(api_key="k")
    manager = ServiceManager(config)
    session = RunSession(token="t", owner_pid=1, provider="codex", heartbeat=0.0)
    owned = cli._OwnedServer(config, manager, session)

    def fail_heartbeat(session: RunSession) -> RunSession:
        raise RuntimeError("Kessel run session ownership was lost")

    monkeypatch.setattr(owned.store, "heartbeat", fail_heartbeat)
    stop_calls: list[bool] = []
    monkeypatch.setattr(manager, "request_stop", lambda: stop_calls.append(True))

    await asyncio.wait_for(owned._heartbeat(), timeout=3)

    assert stop_calls == [True]
    assert "heartbeat failed" in capsys.readouterr().err


def test_server_log_config_survives_uvicorn_dictconfig(tmp_path, monkeypatch) -> None:
    import logging
    import logging.config

    from kessel_gateway import cli
    from kessel_gateway.service import ServiceManager
    from kessel_gateway.user_config import UserConfig

    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    config = cli._server_log_config(ServiceManager(UserConfig()), console=False)
    logging.config.dictConfig(config)
    try:
        logging.getLogger("uvicorn.error").info("server started")
        for handler in logging.getLogger("uvicorn").handlers:
            handler.flush()
        log_path = tmp_path / "state" / "logs" / "kessel.log"
        assert "server started" in log_path.read_text(encoding="utf-8")
    finally:
        for name in ("uvicorn", "uvicorn.access"):
            for handler in logging.getLogger(name).handlers[:]:
                handler.close()
                logging.getLogger(name).removeHandler(handler)


def test_stop_reports_when_nothing_is_running(
    configured: UserConfig, monkeypatch, capsys
) -> None:
    stopped: list[bool] = []

    class IdleServiceManager:
        def __init__(self, config: UserConfig) -> None:
            pass

        def is_running(self) -> bool:
            return False

        def stop(self) -> None:
            stopped.append(True)

    monkeypatch.setattr(cli, "ServiceManager", IdleServiceManager)

    assert cli.main(["stop"]) == 0
    assert capsys.readouterr().out == "Kessel isn't running.\n"
    assert stopped == [True]


def test_key_before_setup_does_not_create_a_key(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))

    assert cli.main(["key"]) == 1
    assert "kessel setup" in capsys.readouterr().err
    assert not UserConfig().path.exists()


def test_env_defaults_to_the_platform_shell(configured: UserConfig, capsys) -> None:
    assert cli.main(["env"]) == 0
    output = capsys.readouterr().out
    expected = "$env:OPENAI_BASE_URL" if os.name == "nt" else "export OPENAI_BASE_URL"
    assert expected in output


def test_run_rejects_missing_application_before_starting_kessel(
    configured: UserConfig, monkeypatch, capsys
) -> None:
    async def unexpected_acquire(config: UserConfig, provider: str):
        raise AssertionError("Kessel must not start for a missing command")

    monkeypatch.setattr(cli, "_acquire_runtime", unexpected_acquire)

    assert (
        cli.main(["run", "--provider", "codex", "--", "kessel-no-such-command"])
        == 1
    )
    assert (
        "Application command not found: kessel-no-such-command"
        in capsys.readouterr().err
    )


def test_port_conflict_is_detected_and_blocks_start(
    configured: UserConfig, monkeypatch, capsys
) -> None:
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        manager = ServiceManager(
            UserConfig(api_key="kessel_test_real_key", port=port)
        )
        assert manager.port_conflict() is True

        monkeypatch.setattr(
            cli,
            "load_or_create_config",
            lambda: (UserConfig(api_key="kessel_test_real_key", port=port), False),
        )
        monkeypatch.setattr(
            ServiceManager,
            "ensure_running",
            lambda self: (_ for _ in ()).throw(AssertionError("must not install")),
        )
        assert cli.main(["start"]) == 1
        assert f"Port {port}" in capsys.readouterr().err
    finally:
        listener.close()

    assert manager.port_conflict() is False
