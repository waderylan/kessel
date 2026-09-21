"""Command-line interface for configuring and connecting to Kessel."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence

from app.providers.health import ProviderHealth, check_providers
from app.service import ServiceError, ServiceManager
from app.user_config import UserConfig, load_or_create_config


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
    if not config.api_key:
        raise RuntimeError("Kessel is not set up. Run: kessel setup")
    return config


def _shell_value(value: str, shell: str) -> str:
    if shell == "powershell":
        return "'" + value.replace("'", "''") + "'"
    if shell == "fish":
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return shlex.quote(value)


def render_env(config: UserConfig, provider: str, shell: str) -> str:
    values = {
        "OPENAI_BASE_URL": f"{config.base_url}/v1/{provider}",
        "OPENAI_API_KEY": config.api_key or "",
        "ANTHROPIC_BASE_URL": config.base_url,
        "ANTHROPIC_API_KEY": config.api_key or "",
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


def render_connect(config: UserConfig, target: str) -> str:
    key = config.api_key or ""
    openai_url = f"{config.base_url}/v1/claude"
    root_url = config.base_url
    snippets = {
        "openai-python": f'''Paste into your Python code:\n\nfrom openai import OpenAI\n\nclient = OpenAI(\n    base_url="{openai_url}",\n    api_key="{key}",\n)\n\nresponse = client.chat.completions.create(\n    model="default",\n    messages=[{{"role": "user", "content": "Hello"}}],\n)''',
        "openai-node": f'''Paste into your Node.js code:\n\nimport OpenAI from "openai";\n\nconst client = new OpenAI({{\n  baseURL: "{openai_url}",\n  apiKey: "{key}",\n}});\n\nconst response = await client.chat.completions.create({{\n  model: "default",\n  messages: [{{ role: "user", content: "Hello" }}],\n}});''',
        "anthropic-python": f'''Paste into your Python code:\n\nfrom anthropic import Anthropic\n\nclient = Anthropic(\n    base_url="{root_url}",\n    api_key="{key}",\n)\n\nmessage = client.messages.create(\n    model="default",\n    max_tokens=256,\n    messages=[{{"role": "user", "content": "Hello"}}],\n)''',
        "anthropic-node": f'''Paste into your Node.js code:\n\nimport Anthropic from "@anthropic-ai/sdk";\n\nconst client = new Anthropic({{\n  baseURL: "{root_url}",\n  apiKey: "{key}",\n}});\n\nconst message = await client.messages.create({{\n  model: "default",\n  max_tokens: 256,\n  messages: [{{ role: "user", content: "Hello" }}],\n}});''',
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
        else:
            print(f"[fix] {check.display_name} isn't logged in.")
            print(f"      Run: {check.fix_command}")


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
            "Authorization": f"Bearer {config.api_key}",
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


def command_setup() -> int:
    print("Checking providers...")
    checks = check_providers()
    _print_doctor(checks)
    working = [check for check in checks if check.working]

    config, created = load_or_create_config()
    resolved_commands = {check.name: check.executable for check in checks}
    configured_commands = UserConfig(
        api_key=config.api_key,
        host=config.host,
        port=config.port,
        codex_command=resolved_commands.get("codex") or config.codex_command,
        claude_command=resolved_commands.get("claude") or config.claude_command,
    )
    if configured_commands != config:
        configured_commands.save()
        config = configured_commands
    print("[ok] Generated a local API key" if created else "[ok] API key already exists")

    manager = ServiceManager(config)
    try:
        changed = manager.ensure_running()
        if not manager.wait_until_running():
            raise ServiceError("the service did not become ready within 20 seconds")
    except ServiceError as exc:
        print(f"[error] Could not start Kessel: {exc}", file=sys.stderr)
        return 1
    print("[ok] Background service installed and started" if changed else "[ok] Background service is already running")

    if not working:
        print("[warning] No provider is ready; install or log in to one using the command above.")
    tests_failed = False
    for check in working:
        success, detail = _test_provider(config, check.name)
        if success:
            print(f"[ok] {check.display_name} test request succeeded: {detail}")
        else:
            print(f"[error] {check.display_name} test request failed: {detail}")
            tests_failed = True

    print("\nConnection details")
    print(f"OpenAI base URL (Claude): {config.base_url}/v1/claude")
    print(f"OpenAI base URL (Codex):  {config.base_url}/v1/codex")
    print(f"Anthropic base URL:       {config.base_url}")
    print("API key:                  run `kessel key` to reveal it")
    return 1 if tests_failed else 0


def command_start() -> int:
    config, _ = load_or_create_config()
    manager = ServiceManager(config)
    try:
        changed = manager.ensure_running()
        if not manager.wait_until_running():
            raise ServiceError("the service did not become ready within 20 seconds")
    except ServiceError as exc:
        print(f"Could not start Kessel: {exc}", file=sys.stderr)
        return 1
    print("Kessel started." if changed else "Kessel is already running.")
    return 0


def command_serve() -> int:
    import uvicorn

    config = UserConfig.load()
    if not (os.getenv("KESSEL_API_KEY") or config.api_key):
        raise RuntimeError("Kessel is not set up. Run: kessel setup")
    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        workers=1,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kessel", description="Local API for Codex and Claude Code subscriptions"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("setup", help="check providers and configure Kessel")
    subparsers.add_parser("doctor", help="check provider installation and login")
    env_parser = subparsers.add_parser("env", help="print client environment variables")
    env_parser.add_argument("--provider", choices=("codex", "claude"), default="claude")
    env_parser.add_argument(
        "--shell", choices=("fish", "powershell"), default="posix"
    )
    connect = subparsers.add_parser("connect", help="print setup for a client")
    connect.add_argument("tool")
    key_parser = subparsers.add_parser("key", help="manage the local API key")
    key_parser.add_argument(
        "--rotate", action="store_true", help="replace the saved API key"
    )
    key_parser.add_argument(
        "--copy", action="store_true", help="copy the key without printing it"
    )
    subparsers.add_parser("start", help="install and start the background service")
    subparsers.add_parser("stop", help="stop the background service")
    subparsers.add_parser("status", help="show whether the service is running")
    subparsers.add_parser("serve", help="run the foreground API server")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "setup":
            return command_setup()
        if args.command == "doctor":
            checks = check_providers()
            _print_doctor(checks)
            return 0 if any(check.working for check in checks) else 1
        if args.command == "env":
            print(render_env(_configured(), args.provider, args.shell))
            return 0
        if args.command == "connect":
            print(render_connect(_configured(), args.tool))
            return 0
        if args.command == "key":
            config, _ = load_or_create_config()
            if args.rotate:
                if os.getenv("KESSEL_API_KEY"):
                    raise RuntimeError(
                        "Cannot rotate while KESSEL_API_KEY overrides the saved key"
                    )
                config = config.with_rotated_key()
                config.save()
                print("Kessel API key rotated. Existing clients must use the new key.")
            if args.copy:
                if not _copy_to_clipboard(config.api_key or ""):
                    raise RuntimeError("Clipboard unavailable")
                print("Kessel API key copied to the clipboard.")
            elif not args.rotate:
                print(config.api_key)
            return 0
        if args.command == "start":
            return command_start()
        if args.command == "stop":
            config = _configured()
            ServiceManager(config).stop()
            print("Kessel stopped.")
            return 0
        if args.command == "status":
            config = _configured()
            if ServiceManager(config).is_running():
                print(f"Kessel is running at {config.base_url}.")
                return 0
            print("Kessel isn't running. Start it with: kessel start", file=sys.stderr)
            return 1
        if args.command == "serve":
            return command_serve()
    except (RuntimeError, ValueError, ServiceError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
