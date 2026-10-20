# Prerequisites and installation

Use installed CLI help and the Kessel release's `kessel doctor` output as the
authority for supported commands and recovery versions. Do not hard-code a
provider version from an old example.

## Detect before installing

Kessel requires Python 3.10+ and pipx. Provider installation via npm also needs
Node.js/npm. Do not replace a working Python or Node installation.

- PowerShell: use `Get-Command python, py, pipx, node, npm, codex, claude, kessel
  -ErrorAction SilentlyContinue` and individual `--version` checks. An absent
  `python` may be a Windows store alias; check the Python launcher too.
- POSIX shells: use `command -v` for each needed executable and targeted version
  checks. Use the actual project interpreter when inspecting a virtualenv.
- Fish uses different environment syntax; do not paste POSIX `export` commands
  into it. Prefer `kessel run` over modifying shell environments.

Choose an OS-appropriate Python/Node installation method using the machine's
existing package manager or official installer. If that needs administrative
access, explain that concrete requirement; prefer a supported user installation
where possible. Do not execute downloaded installation scripts blindly.

## Install pipx and Kessel

Follow the platform's supported pipx installation method. In a Python
installation that supports user packages, `python -m pip install --user pipx`
is an option, followed by `python -m pipx ensurepath`. On externally managed
Python distributions, use the OS package manager; do not use
`--break-system-packages` or `sudo pip`.

`ensurepath` can require a new shell. Use `python -m pipx` temporarily if its
launcher is not visible. Do not overwrite the full PATH or persist Kessel client
variables globally.

```text
pipx install kessel-gateway
kessel doctor
```

If Kessel is installed but not visible, inspect pipx's executable directory and
the current PATH entries only; do not dump the environment. Do not guess that
`python -m kessel_gateway` can access a package isolated in pipx.

## Provider installation and login

| Provider | Install when missing | Login |
| --- | --- | --- |
| Codex | `npm install -g @openai/codex` | `codex login` |
| Claude Code | `npm install -g @anthropic-ai/claude-code` | `claude login` |

If installed help uses a different login command, follow that CLI's supported
interface. The user completes authentication. Verify readiness with
`kessel doctor`; do not inspect the login store. A provider can be installed
and logged in yet fail Kessel's required capability probes. Treat that as a
compatibility problem, not an instruction to reauthenticate repeatedly.

## Setup and persisted files

```text
kessel setup
```

The command does not register a service. With no ready provider it exits
without creating configuration. With ready providers it saves configuration
and performs real, small test requests. A token-counter download can fail
offline; report that requests needing token limits still require that data.

Configuration paths:

- Windows: `%LOCALAPPDATA%\Kessel\config.json`.
- macOS/Linux: `$XDG_CONFIG_HOME/kessel/config.json`, normally
  `~/.config/kessel/config.json`.
- `KESSEL_CONFIG_DIR` overrides the directory; inspect only the needed variable.

Do not display the file: it contains the local key. Use Kessel commands to
create or change configuration with its normal private-file handling.
