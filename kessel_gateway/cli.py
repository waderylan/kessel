"""Command-line interface for configuring and connecting to Kessel."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from kessel_gateway import __version__
from kessel_gateway.models import ProviderAccountInfo
from kessel_gateway.process_security import ProcessGroupGuard
from kessel_gateway.providers.accounts import read_provider_accounts
from kessel_gateway.providers.health import ProviderHealth, check_providers
from kessel_gateway.run_session import (
    RunSession,
    RunSessionStore,
    _process_is_running,
)
from kessel_gateway.service import ServiceError, ServiceManager
from kessel_gateway.user_config import (
    UserConfig,
    effective_api_key,
    load_or_create_config,
)


CONNECT_TARGETS = (
    "openai-python",
    "openai-node",
    "anthropic-python",
    "anthropic-node",
    "curl",
    "cursor",
    "continue",
    "aider",
)


def _configured() -> UserConfig:
    config = UserConfig.load()
    if not effective_api_key(config):
        raise RuntimeError("Kessel is not set up. Run: kessel setup")
    return config


def _default_provider(config: UserConfig) -> str:
    codex_set = bool(config.codex_command)
    claude_set = bool(config.claude_command)
    if codex_set and not claude_set:
        return "codex"
    return "claude"


def _shell_value(value: str, shell: str) -> str:
    if shell == "powershell":
        return "'" + value.replace("'", "''") + "'"
    if shell == "fish":
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return shlex.quote(value)


def render_env(config: UserConfig, provider: str, shell: str) -> str:
    key = effective_api_key(config) or ""
    values = {
        "OPENAI_BASE_URL": f"{config.base_url}/v1/{provider}",
        "OPENAI_API_KEY": key,
        "ANTHROPIC_BASE_URL": config.base_url,
        "ANTHROPIC_API_KEY": key,
    }
    if shell == "powershell":
        return "\n".join(
            f"$env:{name} = {_shell_value(value, shell)}"
            for name, value in values.items()
        )
    if shell == "fish":
        return "\n".join(
            f"set -gx {name} {_shell_value(value, shell)};"
            for name, value in values.items()
        )
    return "\n".join(
        f"export {name}={_shell_value(value, shell)}"
        for name, value in values.items()
    )


def render_connect(config: UserConfig, provider: str, target: str) -> str:
    key = effective_api_key(config) or ""
    openai_url = f"{config.base_url}/v1/{provider}"
    root_url = config.base_url
    anthropic_note = (
        "\n\nNote: Anthropic SDK routes always use Claude Code."
        if provider == "codex"
        else ""
    )
    snippets = {
        "openai-python": f'''Paste into your Python code:\n\nfrom openai import OpenAI\n\nclient = OpenAI(\n    base_url="{openai_url}",\n    api_key="{key}",\n)\n\nresponse = client.chat.completions.create(\n    model="default",\n    messages=[{{"role": "user", "content": "Hello"}}],\n)''',
        "openai-node": f'''Paste into your Node.js code:\n\nimport OpenAI from "openai";\n\nconst client = new OpenAI({{\n  baseURL: "{openai_url}",\n  apiKey: "{key}",\n}});\n\nconst response = await client.chat.completions.create({{\n  model: "default",\n  messages: [{{ role: "user", content: "Hello" }}],\n}});''',
        "anthropic-python": f'''Paste into your Python code:\n\nfrom anthropic import Anthropic\n\nclient = Anthropic(\n    base_url="{root_url}",\n    api_key="{key}",\n)\n\nmessage = client.messages.create(\n    model="default",\n    max_tokens=256,\n    messages=[{{"role": "user", "content": "Hello"}}],\n){anthropic_note}''',
        "anthropic-node": f'''Paste into your Node.js code:\n\nimport Anthropic from "@anthropic-ai/sdk";\n\nconst client = new Anthropic({{\n  baseURL: "{root_url}",\n  apiKey: "{key}",\n}});\n\nconst message = await client.messages.create({{\n  model: "default",\n  max_tokens: 256,\n  messages: [{{ role: "user", content: "Hello" }}],\n}});{anthropic_note}''',
        "curl": f'''Paste into a terminal:\n\ncurl {openai_url}/chat/completions \\\n  -H "Authorization: Bearer {key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{{"model":"default","messages":[{{"role":"user","content":"Hello"}}]}}' ''',
        "cursor": f'''Paste these values in Cursor Settings > Models > Add Custom Model:\n\nModel name: default\nOverride OpenAI Base URL: {openai_url}\nOpenAI API Key: {key}''',
        "continue": f'''Paste into ~/.continue/config.yaml:\n\nname: Kessel\nversion: 0.0.1\nschema: v1\nmodels:\n  - name: Kessel Claude\n    provider: openai\n    model: default\n    apiBase: {openai_url}\n    apiKey: {key}''',
        "aider": f'''Paste into a terminal before running Aider:\n\nexport OPENAI_API_BASE={openai_url}\nexport OPENAI_API_KEY={key}\naider --model openai/default''',
    }
    if os.name == "nt":
        snippets["curl"] = f'''Paste into PowerShell:\n\ncurl.exe "{openai_url}/chat/completions" `\n  -H "Authorization: Bearer {key}" `\n  -H "Content-Type: application/json" `\n  -d '{{"model":"default","messages":[{{"role":"user","content":"Hello"}}]}}' '''
        snippets["aider"] = f'''Paste into PowerShell before running Aider:\n\n$env:OPENAI_API_BASE = "{openai_url}"\n$env:OPENAI_API_KEY = "{key}"\naider --model openai/default'''
    if target in snippets:
        return snippets[target]
    return (
        f"Paste these values into {target}:\n\n"
        f"OpenAI-compatible base URL: {openai_url}\n"
        f"API key: {key}\n"
        "Authentication header: Authorization: Bearer <API key>"
    )


def _print_doctor(checks: Sequence[ProviderHealth]) -> None:
    for check in checks:
        if check.working:
            suffix = f" ({check.version})" if check.version else ""
            print(f"[ok] {check.display_name} is installed and logged in{suffix}")
        elif not check.installed:
            print(f"[fix] {check.display_name} is not installed.")
            print(f"      Run: {check.fix_command}")
        elif not check.compatible:
            suffix = f" ({check.version})" if check.version else ""
            print(f"[fix] {check.display_name} is incompatible{suffix}.")
            if check.detail:
                print(f"      {check.detail}")
            print(f"      Known stable version: {check.known_stable_version}")
            print(f"      Run: {check.fix_command}")
        else:
            print(f"[fix] {check.display_name} isn't logged in.")
            print(f"      Run: {check.fix_command}")


def _print_provider_accounts(accounts: Sequence[ProviderAccountInfo]) -> None:
    display_names = {"codex": "Codex", "claude": "Claude Code"}
    for account in accounts:
        name = display_names.get(account.provider, account.provider)
        if account.status == "not_installed":
            print(f"[missing] {name} account: provider is not installed")
            continue
        if account.status == "not_authenticated":
            print(f"[signed out] {name} account: provider is not signed in")
            continue
        if account.status != "authenticated":
            print(f"[unavailable] {name} account: details could not be read")
            continue

        parts = []
        if account.email:
            parts.append(account.email)
        if account.organization and account.organization not in parts:
            parts.append(account.organization)
        if account.subscription:
            parts.append(f"plan={account.subscription}")
        if account.auth_method:
            parts.append(f"auth={account.auth_method}")
        print(f"[ok] {name} account: " + " | ".join(parts or ["signed in"]))


def command_accounts() -> int:
    checks = check_providers()
    accounts = asyncio.run(read_provider_accounts(checks))
    _print_provider_accounts(accounts)
    return 0


def _test_provider(config: UserConfig, provider: str) -> tuple[bool, str]:
    payload = json.dumps(
        {
            "model": "default",
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{config.base_url}/v1/{provider}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {effective_api_key(config)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        return True, str(text).strip()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body).get("error", {}).get("message", body)
        except json.JSONDecodeError:
            message = body
        return False, str(message).strip()
    except (OSError, urllib.error.URLError, KeyError, ValueError) as exc:
        return False, str(exc)


def _copy_to_clipboard(value: str) -> bool:
    commands = []
    if sys.platform == "win32":
        commands = [["clip"]]
    elif sys.platform == "darwin":
        commands = [["pbcopy"]]
    elif os.getenv("WAYLAND_DISPLAY"):
        commands = [["wl-copy"], ["xclip", "-selection", "clipboard"]]
    else:
        commands = [
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    for command in commands:
        try:
            result = subprocess.run(
                command,
                input=value,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
                shell=False,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False


def client_environment(config: UserConfig, provider: str) -> dict[str, str]:
    """Return the current environment with Kessel client settings injected."""

    key = effective_api_key(config) or ""
    environment = os.environ.copy()
    environment.update(
        {
            "OPENAI_BASE_URL": f"{config.base_url}/v1/{provider}",
            "OPENAI_API_KEY": key,
            "ANTHROPIC_BASE_URL": config.base_url,
            "ANTHROPIC_API_KEY": key,
        }
    )
    return environment


def _managed_process_options(*, hidden: bool) -> dict[str, object]:
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if hidden:
            flags |= subprocess.CREATE_NO_WINDOW
        return {"creationflags": flags}
    return {"start_new_session": True}


def _tail_log(path: Path, lines: int = 20) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


def _startup_errors(path: Path, lines: int = 5) -> str:
    """Return the most recent error lines, or a short tail when there are none."""

    tail = _tail_log(path, 200).splitlines()
    errors = [line for line in tail if " ERROR " in line or " CRITICAL " in line]
    return "\n".join(errors[-lines:] if errors else tail[-lines:])


class _OwnedServer:
    """A temporary API server whose lifetime belongs to this CLI process."""

    def __init__(
        self, config: UserConfig, manager: ServiceManager, session: RunSession
    ) -> None:
        self.config = config
        self.manager = manager
        self.session = session
        self.store = RunSessionStore()
        self.process: asyncio.subprocess.Process | None = None
        self.guard: ProcessGroupGuard | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.manager.clear_stop_request()
        self.heartbeat_task = asyncio.create_task(self._heartbeat())
        try:
            owner_environment = os.environ.copy()
            owner_environment["KESSEL_OWNER_PID"] = str(os.getpid())
            self.process = await asyncio.create_subprocess_exec(
                *self.manager.command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=owner_environment,
                **_managed_process_options(hidden=True),
            )
            self.guard = ProcessGroupGuard.attach(self.process)
            deadline = asyncio.get_running_loop().time() + 20
            while asyncio.get_running_loop().time() < deadline:
                if await asyncio.to_thread(self.manager.is_running):
                    # A different server may have won a simultaneous startup.
                    # Confirm this owned process remains alive after the socket
                    # has had time to reject a competing bind.
                    await asyncio.sleep(0.5)
                    if self.process.returncode is None:
                        return
                    raise ServiceError(
                        "another Kessel server started at the configured address"
                    )
                if self.process.returncode is not None:
                    raise ServiceError(
                        f"temporary server exited with code {self.process.returncode}"
                        f"{self._log_tail_suffix()}"
                    )
                await asyncio.sleep(0.2)
            raise ServiceError(
                "the temporary server did not become ready within 20 seconds"
                f"{self._log_tail_suffix()}"
            )
        except BaseException:
            await self.stop()
            raise

    def _log_tail_suffix(self) -> str:
        tail = _startup_errors(self.manager.log_directory / "kessel.log")
        return f"\n{tail}" if tail else ""

    async def _heartbeat(self) -> None:
        session = self.session
        while True:
            await asyncio.sleep(1)
            try:
                session = await asyncio.to_thread(self.store.heartbeat, session)
            except RuntimeError as exc:
                print(f"Kessel run session heartbeat failed: {exc}", file=sys.stderr)
                await asyncio.to_thread(self.manager.request_stop)
                return
            self.session = session

    async def wait(self, shutdown: asyncio.Event) -> None:
        if self.process is None:
            return
        server_wait = asyncio.create_task(self.process.wait())
        shutdown_wait = asyncio.create_task(shutdown.wait())
        try:
            done, _ = await asyncio.wait(
                {server_wait, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if server_wait in done and not shutdown.is_set():
                return_code = server_wait.result()
                if return_code != 0:
                    raise ServiceError(
                        f"temporary server exited with code {return_code}"
                    )
        finally:
            for task in (server_wait, shutdown_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(server_wait, shutdown_wait, return_exceptions=True)

    async def stop(self) -> None:
        heartbeat = self.heartbeat_task
        self.heartbeat_task = None
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        process = self.process
        if process is not None and process.returncode is None:
            await asyncio.to_thread(self.manager.request_stop)
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                if self.guard is not None:
                    self.guard.terminate()
                else:
                    process.kill()
                await process.wait()
        if self.guard is not None:
            await self.guard.close()
            self.guard = None
        self.process = None
        self.manager.clear_stop_request()
        await asyncio.to_thread(self.store.release, self.session.token)


async def _acquire_runtime(
    config: UserConfig, provider: str
) -> tuple[str, _OwnedServer | None, RunSession | None]:
    """Find a live runtime or start one owned by this process."""

    manager = ServiceManager(config)
    store = RunSessionStore()
    if await asyncio.to_thread(manager.is_running):
        session = await asyncio.to_thread(store.active)
        if session is not None:
            return "foreground", None, session
        if await asyncio.to_thread(manager.is_registered):
            return "durable", None, None
        return "existing", None, None
    if await asyncio.to_thread(manager.port_conflict):
        raise ServiceError(manager.port_conflict_message())

    session = await asyncio.to_thread(store.claim, provider)
    if session is None:
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            active = await asyncio.to_thread(store.active)
            if active is None:
                break
            if await asyncio.to_thread(manager.is_running):
                return "foreground", None, active
            await asyncio.sleep(0.2)
        raise ServiceError("another Kessel run session did not become ready")

    owned = _OwnedServer(config, manager, session)
    await owned.start()
    return "temporary", owned, session


async def _run_application(
    config: UserConfig,
    provider: str,
    command: Sequence[str],
    expected_session: RunSession | None,
    shutdown: asyncio.Event,
) -> int:
    resolved_command = list(command)
    resolved_executable = shutil.which(resolved_command[0])
    if resolved_executable:
        resolved_command[0] = resolved_executable
    try:
        # No new process group or session: the application stays in
        # Kessel's own group so the terminal's Ctrl+C, SIGHUP, and /dev/tty
        # reach it directly, the same as any other foreground command.
        process = await asyncio.create_subprocess_exec(
            *resolved_command,
            env=client_environment(config, provider),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Application command not found: {command[0]}") from exc

    # The Windows job object is safe to use here because it targets exactly
    # this process and its descendants. On POSIX the application was not
    # given its own session, so killpg (what ProcessGroupGuard uses) would
    # hit Kessel's own group; terminate the application's PID directly
    # instead.
    guard = ProcessGroupGuard.attach(process) if os.name == "nt" else None

    loop = asyncio.get_running_loop()
    sigint_installed = False
    previous_sigint_handler = None
    if os.name != "nt":
        try:
            loop.add_signal_handler(signal.SIGINT, lambda: None)
            sigint_installed = True
        except NotImplementedError:
            pass
    else:
        previous_sigint_handler = signal.signal(
            signal.SIGINT, lambda signum, frame: None
        )

    async def terminate_application() -> None:
        if process.returncode is not None:
            return
        if guard is not None:
            guard.terminate()
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def runtime_stopped() -> None:
        manager = ServiceManager(config)
        store = RunSessionStore()
        while process.returncode is None:
            if not await asyncio.to_thread(manager.is_running):
                return
            if expected_session is not None:
                active = await asyncio.to_thread(store.active)
                if active is None or active.token != expected_session.token:
                    return
            await asyncio.sleep(0.5)

    process_wait = asyncio.create_task(process.wait())
    runtime_wait = asyncio.create_task(runtime_stopped())
    shutdown_wait = asyncio.create_task(shutdown.wait())
    try:
        done, _ = await asyncio.wait(
            {process_wait, runtime_wait, shutdown_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if (
            runtime_wait in done or shutdown_wait in done
        ) and process.returncode is None:
            print("Kessel stopped; ending the managed application.", file=sys.stderr)
            await terminate_application()
            return 1
        return process_wait.result()
    finally:
        for task in (process_wait, runtime_wait, shutdown_wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            process_wait, runtime_wait, shutdown_wait, return_exceptions=True
        )
        await terminate_application()
        if guard is not None:
            await guard.close()
        if os.name != "nt":
            if sigint_installed:
                loop.remove_signal_handler(signal.SIGINT)
        else:
            signal.signal(signal.SIGINT, previous_sigint_handler)


async def _run(config: UserConfig, provider: str, command: Sequence[str]) -> int:
    # Fail before starting a server for an application that cannot run.
    if command and shutil.which(command[0]) is None:
        raise RuntimeError(f"Application command not found: {command[0]}")

    # Installed for the entire lifetime of this command, including while an
    # application runs, so a closed terminal (SIGHUP) or SIGTERM triggers the
    # same graceful cleanup as Ctrl+C instead of leaving Kessel or the
    # application orphaned.
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    installed_signals: list[signal.Signals] = []
    if os.name != "nt":
        for signum in (signal.SIGHUP, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, shutdown.set)
                installed_signals.append(signum)
            except NotImplementedError:
                pass

    try:
        kind, owned, session = await _acquire_runtime(config, provider)
        if kind == "durable":
            print(
                f"Durable Kessel detected at {config.base_url}; reusing it.",
                flush=True,
            )
        elif kind == "foreground":
            print(
                f"Foreground Kessel run session detected at {config.base_url}; "
                "attaching.",
                flush=True,
            )
        elif kind == "existing":
            print(
                f"Existing Kessel server detected at {config.base_url}; reusing it.",
                flush=True,
            )
        else:
            print(
                f"Temporary Kessel started at {config.base_url}; "
                f"client provider: {provider}.",
                flush=True,
            )

        try:
            if command:
                return await _run_application(
                    config, provider, command, session, shutdown
                )
            if owned is None:
                print(
                    "No application command was supplied; Kessel remains owned "
                    "elsewhere."
                )
                return 0
            print("Keep this terminal open. Press Ctrl+C to stop Kessel.")
            print(
                "Run an application from another terminal with: "
                f"kessel run --provider {provider} -- <command>"
            )
            await owned.wait(shutdown)
            return 0
        finally:
            if owned is not None:
                await owned.stop()
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)


async def _setup_provider_tests(
    config: UserConfig, working: Sequence[ProviderHealth]
) -> bool:
    if not working:
        return False
    accounts = await read_provider_accounts(working)
    print("Provider accounts")
    _print_provider_accounts(accounts)
    kind, owned, _ = await _acquire_runtime(config, working[0].name)
    if kind == "temporary":
        print("[ok] Temporary Kessel started for provider tests")
    elif kind == "durable":
        print("[ok] Durable Kessel detected; using it for provider tests")
    elif kind == "foreground":
        print("[ok] Foreground Kessel run session detected; using it for provider tests")
    else:
        print("[ok] Existing Kessel server detected; using it for provider tests")
    tests_failed = False
    try:
        for check in working:
            success, detail = await asyncio.to_thread(
                _test_provider, config, check.name
            )
            if success:
                print(f"[ok] {check.display_name} test request succeeded: {detail}")
            else:
                print(f"[error] {check.display_name} test request failed: {detail}")
                tests_failed = True
    finally:
        if owned is not None:
            await owned.stop()
            print("[ok] Temporary Kessel stopped")
    return tests_failed


def command_setup(port: int | None = None) -> int:
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")

    print("Checking providers...")
    checks = check_providers()
    _print_doctor(checks)
    working = [check for check in checks if check.working]

    if not working:
        print(
            "[error] Kessel needs at least one installed and logged-in provider.",
            file=sys.stderr,
        )
        print(
            "Install or log in to Codex or Claude Code using the guidance above, "
            "then run `kessel setup` again.",
            file=sys.stderr,
        )
        return 1

    if len(working) == 1:
        print(
            f"[ok] {working[0].display_name} is ready. Kessel will use that "
            "provider; the other provider is optional."
        )
    else:
        print("[ok] Codex and Claude Code are both ready.")

    config, created = load_or_create_config()
    resolved_commands = {check.name: check.executable for check in working}
    configured_commands = UserConfig(
        api_key=config.api_key,
        host=config.host,
        port=port if port is not None else config.port,
        codex_command=resolved_commands.get("codex") or config.codex_command,
        claude_command=resolved_commands.get("claude") or config.claude_command,
    )
    if configured_commands != config:
        configured_commands.save()
        config = configured_commands
    print("[ok] Generated a local API key" if created else "[ok] API key already exists")

    try:
        from kessel_gateway.output_control import estimate_tokens

        estimate_tokens("warmup")
        print("[ok] Token counter ready")
    except Exception:
        print(
            "[warn] Token counter data could not be downloaded; requests with "
            "max_tokens need it once online"
        )

    try:
        tests_failed = asyncio.run(_setup_provider_tests(config, working))
    except ServiceError as exc:
        print(f"[error] Could not test Kessel: {exc}", file=sys.stderr)
        return 1

    print("\nConnection details")
    working_names = {check.name for check in working}
    if "claude" in working_names:
        print(f"OpenAI base URL (Claude): {config.base_url}/v1/claude")
        print(f"Anthropic base URL:       {config.base_url}")
    if "codex" in working_names:
        print(f"OpenAI base URL (Codex):  {config.base_url}/v1/codex")
    print("API key:                  run `kessel key` to reveal it")
    print("\nRun an application with managed settings:")
    print(
        f"kessel run --provider {working[0].name} -- python your_app.py"
    )
    print("Setup does not leave Kessel running in the background.")
    return 1 if tests_failed else 0


def command_start() -> int:
    config, _ = load_or_create_config()
    manager = ServiceManager(config)
    if manager.is_running():
        if RunSessionStore().active() is not None:
            print(
                "A foreground Kessel run session is active. Stop it before "
                "starting durable Kessel.",
                file=sys.stderr,
            )
            return 1
        if manager.is_registered():
            print("Durable Kessel is already running.")
            return 0
        print(
            "An unmanaged Kessel server is already using the configured address.",
            file=sys.stderr,
        )
        return 1
    if manager.port_conflict():
        print(manager.port_conflict_message(), file=sys.stderr)
        return 1
    try:
        changed = manager.ensure_running()
        if not manager.wait_until_running():
            raise ServiceError("the service did not become ready within 20 seconds")
    except ServiceError as exc:
        print(f"Could not start Kessel: {exc}", file=sys.stderr)
        return 1
    print("Durable Kessel started." if changed else "Durable Kessel is already running.")
    return 0


def _server_log_config(
    manager: ServiceManager, *, console: bool
) -> dict[str, object]:
    """Return a uvicorn log config that also writes a rotating file.

    uvicorn applies its log config with ``dictConfig``, which replaces any
    handler attached beforehand, so the file handler must be part of the
    config itself. Never route prompts, responses, keys, or account info
    here; the request logs only carry method/path/status.
    """

    log_dir = manager.log_directory
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = log_dir / "kessel.log"
    log_path.touch(exist_ok=True)
    if os.name != "nt":
        log_dir.chmod(0o700)
        log_path.chmod(0o600)

    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config["formatters"]["file"] = {
        "format": "%(asctime)s %(levelname)s %(name)s: %(message)s"
    }
    config["handlers"]["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "formatter": "file",
        "filename": str(log_path),
        "maxBytes": 1_048_576,
        "backupCount": 3,
        "encoding": "utf-8",
    }
    # Durable services have no terminal; console output there only grows the
    # service manager's unrotated stdout/stderr files.
    for name, handlers in (
        ("uvicorn", ["default"]),
        ("uvicorn.access", ["access"]),
    ):
        logger = config["loggers"][name]
        logger["handlers"] = (handlers if console else []) + ["file"]
    return config


def command_serve() -> int:
    import uvicorn

    config = UserConfig.load()
    if not effective_api_key(config):
        raise RuntimeError("Kessel is not set up. Run: kessel setup")
    manager = ServiceManager(config)
    if manager.port_conflict():
        raise RuntimeError(manager.port_conflict_message())
    manager.clear_stop_request()
    console = sys.stderr is not None and sys.stderr.isatty()
    uvicorn_config = uvicorn.Config(
        "kessel_gateway.main:app",
        host=config.host,
        port=config.port,
        workers=1,
        log_config=_server_log_config(manager, console=console),
    )
    server = uvicorn.Server(uvicorn_config)
    watcher_done = threading.Event()

    owner_pid_raw = os.getenv("KESSEL_OWNER_PID")
    owner_pid = (
        int(owner_pid_raw)
        if owner_pid_raw is not None and owner_pid_raw.isdigit()
        else None
    )

    def watch_for_stop() -> None:
        while not watcher_done.wait(0.5):
            if manager.stop_request_path.exists():
                server.should_exit = True
                return
            if owner_pid is not None and not _process_is_running(owner_pid):
                server.should_exit = True
                return

    watcher = threading.Thread(
        target=watch_for_stop,
        name="kessel-stop-watcher",
        daemon=True,
    )
    watcher.start()
    try:
        server.run()
    finally:
        watcher_done.set()
        watcher.join(timeout=1)
        manager.clear_stop_request()
    return 0


def command_logs(lines: int) -> int:
    config = UserConfig.load()
    manager = ServiceManager(config)
    log_path = manager.log_directory / "kessel.log"
    if not log_path.exists():
        print("No log file yet. Kessel writes one once it has run as a server.", file=sys.stderr)
        return 1
    print(_tail_log(log_path, lines))
    return 0


def command_uninstall_service() -> int:
    config = UserConfig.load()
    manager = ServiceManager(config)
    manager.uninstall()
    print("Kessel's durable service registration is removed.")
    print(f"Configuration and API key remain at {config.path}.")
    print(f"Logs remain at {manager.log_directory}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kessel", description="Local API for Codex and Claude Code subscriptions"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup_parser = subparsers.add_parser(
        "setup", help="check providers and configure Kessel"
    )
    setup_parser.add_argument(
        "--port", type=int, default=None, help="set the port Kessel listens on"
    )
    subparsers.add_parser("accounts", help="show provider account information")
    subparsers.add_parser("doctor", help="check provider installation and login")
    env_parser = subparsers.add_parser("env", help="print client environment variables")
    env_parser.add_argument("--provider", choices=("codex", "claude"), default=None)
    env_parser.add_argument(
        "--shell",
        choices=("posix", "fish", "powershell"),
        default="powershell" if os.name == "nt" else "posix",
    )
    connect = subparsers.add_parser("connect", help="print setup for a client")
    connect.add_argument("tool")
    connect.add_argument("--provider", choices=("codex", "claude"), default=None)
    key_parser = subparsers.add_parser("key", help="manage the local API key")
    key_parser.add_argument(
        "--rotate", action="store_true", help="replace the saved API key"
    )
    key_parser.add_argument(
        "--copy", action="store_true", help="copy the key without printing it"
    )
    subparsers.add_parser("start", help="install and start durable Kessel")
    subparsers.add_parser("stop", help="stop the current Kessel server")
    subparsers.add_parser("status", help="show whether Kessel is running")
    run_parser = subparsers.add_parser(
        "run", help="run Kessel for the lifetime of an application or terminal"
    )
    run_parser.add_argument(
        "--provider", choices=("codex", "claude"), required=True
    )
    run_parser.add_argument(
        "application",
        nargs=argparse.REMAINDER,
        help="application command, normally placed after --",
    )
    subparsers.add_parser("serve", help="run the foreground API server")
    logs_parser = subparsers.add_parser("logs", help="print the tail of the server log")
    logs_parser.add_argument(
        "-n", type=int, default=20, dest="lines", help="number of lines to print"
    )
    subparsers.add_parser(
        "uninstall-service",
        help="remove the durable Kessel service registration",
    )
    return parser


def _degraded_detail(payload: dict) -> str:
    providers = payload.get("providers")
    unavailable: list[str] = []
    if isinstance(providers, dict):
        for name, info in providers.items():
            available = info.get("available") if isinstance(info, dict) else bool(info)
            if not available:
                unavailable.append(name)
    if unavailable:
        return f"{', '.join(sorted(unavailable))} unavailable"
    return "no provider available"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "setup":
            return command_setup(args.port)
        if args.command == "accounts":
            return command_accounts()
        if args.command == "doctor":
            checks = check_providers()
            _print_doctor(checks)
            return 0 if any(check.working for check in checks) else 1
        if args.command == "env":
            config = _configured()
            provider = args.provider or _default_provider(config)
            print(render_env(config, provider, args.shell))
            return 0
        if args.command == "connect":
            config = _configured()
            provider = args.provider or _default_provider(config)
            print(render_connect(config, provider, args.tool))
            return 0
        if args.command == "key":
            config = _configured()
            if args.rotate:
                if os.getenv("KESSEL_API_KEY"):
                    raise RuntimeError(
                        "Cannot rotate while KESSEL_API_KEY overrides the saved key"
                    )
                config = config.with_rotated_key()
                config.save()
                print("Kessel API key rotated. Existing clients must use the new key.")
            if args.copy:
                if not _copy_to_clipboard(effective_api_key(config) or ""):
                    raise RuntimeError("Clipboard unavailable")
                print("Kessel API key copied to the clipboard.")
            elif not args.rotate:
                print(effective_api_key(config))
            return 0
        if args.command == "start":
            return command_start()
        if args.command == "stop":
            config = _configured()
            manager = ServiceManager(config)
            was_running = manager.is_running()
            session = RunSessionStore().active()
            if session is not None and was_running:
                manager.request_stop()
                if not manager.wait_until_stopped():
                    raise ServiceError("Kessel did not stop within 15 seconds")
            else:
                manager.stop()
            print("Kessel stopped." if was_running else "Kessel isn't running.")
            return 0
        if args.command == "status":
            config = _configured()
            payload = ServiceManager(config).health()
            if payload is not None and payload.get("service") == "kessel":
                if payload.get("status") == "degraded":
                    print(
                        f"Kessel is running at {config.base_url} "
                        f"(degraded: {_degraded_detail(payload)})."
                    )
                else:
                    print(f"Kessel is running at {config.base_url}.")
                return 0
            print(
                "Kessel isn't running. Use: kessel run --provider codex "
                "or kessel start",
                file=sys.stderr,
            )
            return 1
        if args.command == "run":
            application = list(args.application)
            if application[:1] == ["--"]:
                application = application[1:]
            return asyncio.run(_run(_configured(), args.provider, application))
        if args.command == "serve":
            return command_serve()
        if args.command == "logs":
            return command_logs(args.lines)
        if args.command == "uninstall-service":
            return command_uninstall_service()
    except KeyboardInterrupt:
        print("Kessel stopped.")
        return 130
    except (RuntimeError, ValueError, ServiceError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
