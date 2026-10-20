# Select a startup mode

| User need | Command | Lifetime |
| --- | --- | --- |
| One launched application | `kessel run --provider codex -- python app.py` | Owned temporary server stops when the application exits |
| Server available while building an application | `kessel run --provider codex` | Owner terminal stays open; Ctrl+C stops it and attached applications |
| Independently launched editor or multiple tools | `kessel start` | Durable per-user service, including login startup registration |

Replace `codex` with `claude` when selected. `kessel run` supplies client
variables to its child only and reuses an existing Kessel service. A reused
durable service stays running after the application exits. State which case
applies. Do not leave an unowned foreground process running through an agent's
background shell as a substitute for choosing a mode.

The OpenAI-compatible URL is `<configured base URL>/v1/codex` or `/v1/claude`.
The Anthropic base URL is the root configured URL and always uses Claude Code.
The default root URL is `http://127.0.0.1:4880`; use the actual configured port.

Before `kessel start`, explain that it registers login startup and make sure
background operation is within the user's request. It uses the Windows user
startup entry, a macOS launch agent, or a Linux systemd user service.

`kessel stop` stops the current process but retains login registration.
`kessel uninstall-service` stops it and removes that registration while
preserving configuration, key, and logs. Give the latter as the way to undo
durable startup, rather than describing `stop` as disabling autostart.

Starting or stopping an owner can affect other attached applications. Check
existing status and ownership before replacing an active mode. If systemd user
services are unavailable, offer app-managed or foreground operation.
