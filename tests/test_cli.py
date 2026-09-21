from pathlib import Path

import pytest

from app import cli
from app.providers.health import ProviderHealth
from app.user_config import UserConfig


@pytest.fixture
def configured(tmp_path: Path, monkeypatch) -> UserConfig:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    config = UserConfig(api_key="kessel_test_real_key")
    config.save()
    return config


@pytest.mark.parametrize(
    ("shell", "expected"),
    [
        ("posix", "export OPENAI_BASE_URL=http://127.0.0.1:8000/v1/claude"),
        ("fish", "set -gx OPENAI_BASE_URL 'http://127.0.0.1:8000/v1/claude';"),
        (
            "powershell",
            "$env:OPENAI_BASE_URL = 'http://127.0.0.1:8000/v1/claude'",
        ),
    ],
)
def test_env_output_for_each_shell(
    configured: UserConfig, shell: str, expected: str
) -> None:
    output = cli.render_env(configured, "claude", shell)

    assert expected in output
    assert "kessel_test_real_key" in output
    assert "ANTHROPIC_BASE_URL" in output


def test_env_provider_selects_openai_route(configured: UserConfig) -> None:
    output = cli.render_env(configured, "codex", "posix")

    assert "OPENAI_BASE_URL=http://127.0.0.1:8000/v1/codex" in output
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:8000" in output


@pytest.mark.parametrize("target", cli.CONNECT_TARGETS)
def test_each_connect_target_renders_real_key_and_url(
    configured: UserConfig, target: str
) -> None:
    output = cli.render_connect(configured, target)

    assert "kessel_test_real_key" in output
    assert "http://127.0.0.1:8000" in output
    assert "Paste" in output


def test_unknown_connect_target_gets_generic_pair(configured: UserConfig) -> None:
    output = cli.render_connect(configured, "my-tool")

    assert "OpenAI-compatible base URL: http://127.0.0.1:8000/v1/claude" in output
    assert "API key: kessel_test_real_key" in output


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
    assert "kessel run --provider codex -- python your_app.py" in first_output
    assert "does not leave Kessel running" in first_output

    assert cli.main(["setup"]) == 0
    second_output = capsys.readouterr().out
    assert "API key already exists" in second_output
    assert UserConfig.load().api_key == first_key
    assert len(test_calls) == 2


def test_client_environment_injects_kessel_values(
    configured: UserConfig, monkeypatch
) -> None:
    monkeypatch.setenv("KEEP_ME", "yes")

    environment = cli.client_environment(configured, "codex")

    assert environment["KEEP_ME"] == "yes"
    assert environment["OPENAI_BASE_URL"].endswith("/v1/codex")
    assert environment["OPENAI_API_KEY"] == "kessel_test_real_key"
    assert environment["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8000"
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

        def is_running(self) -> bool:
            return False

    monkeypatch.setattr(cli, "ServiceManager", StoppedServiceManager)

    assert cli.main(["status"]) == 1
    assert (
        capsys.readouterr().err
        == "Kessel isn't running. Use: kessel run --provider codex or kessel start\n"
    )
