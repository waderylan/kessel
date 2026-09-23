from pathlib import Path

import pytest

from kessel_gateway import cli
from kessel_gateway import __version__
from kessel_gateway.models import ProviderAccountInfo
from kessel_gateway.providers.health import ProviderHealth
from kessel_gateway.user_config import UserConfig


@pytest.fixture
def configured(tmp_path: Path, monkeypatch) -> UserConfig:
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KESSEL_STATE_DIR", str(tmp_path / "state"))
    config = UserConfig(api_key="kessel_test_real_key")
    config.save()
    return config


def test_cli_version(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"kessel {__version__}"


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
