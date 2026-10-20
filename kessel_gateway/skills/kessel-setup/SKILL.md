---
name: kessel-setup
description: Install and configure Kessel for the first time, guide provider login, and verify a local request. Use when the user asks to get Kessel running on their machine.
---

# Kessel setup

Get the user's local gateway ready and prove that a provider request works.
Kessel exposes the user's existing Codex or Claude Code CLI login through a
localhost API. One working provider is enough.

## Make the work visible

Before changing anything, report the detected OS and shell, available provider,
and missing prerequisites. State the packages to install, configuration to
create, and test request to send. Explain that the test uses provider quota and
that `kessel setup` stops its temporary server afterward.

Honor explicit user instructions over this workflow. Continue work already
authorized by the setup request. Ask only for a missing choice or additional
scope, such as replacing a working provider version or enabling login startup.
Installing this skill alone is not a request to set up Kessel.

## Scope

- Install missing setup prerequisites and Kessel for the current user.
- Guide the user through the official CLI login; they complete browser or
  terminal authentication themselves. Never request passwords or tokens.
- Preserve existing configuration, keys, provider settings, and running sessions.
- Default to app-managed startup. Register a durable service only when the user
  requests background availability or agrees it is needed for their client.
- Application edits belong to a request to connect that application. If setup
  includes that request, continue into connection; otherwise report readiness
  and the launch command without changing the project.

## Workflow

1. Detect the actual OS, shell, Python version, pipx, and provider executables.
   Use targeted executable/version checks, not environment dumps or credential
   files. If Kessel exists, run `kessel --version` and `kessel doctor` first.
2. Follow [installation](references/installation.md) for missing prerequisites.
   Prefer a provider already working on the machine. If neither works and the
   user has not selected one, ask which CLI/account they intend to use.
   Do not install both by default or change subscriptions.
3. Install Kessel with `pipx install kessel-gateway` if missing. Resolve PATH
   visibility in the current shell instead of repeatedly reinstalling packages.
   For existing Kessel, preserve its version unless an update is requested or
   needed and authorized. Check installed `--help` before using unfamiliar flags.
4. Run `kessel doctor`. It checks both providers; a nonzero exit can mean only
   the unused provider is missing. Read the individual results. Use the exact
   compatibility recovery command it reports. Explain a version replacement;
   ask only if it falls outside the authorized setup work. Never disable
   capability checks.
5. Run `kessel setup`. It preserves an existing key, records executable paths,
   pre-warms token counting, and tests every ready provider. Use `--port PORT`
   only for a requested port or an identified conflict. Do not kill a process
   occupying that port. Do not claim success if the selected provider test fails.
6. Report the successful provider and configured base URL from command output.
   Setup alone leaves no server running. For an application, show
   `kessel run --provider codex -- <application command>` (substitute `claude`
   when selected). For an independently launched editor, read
   [startup modes](references/startup.md) before configuring background operation.
7. If the user also asked to connect a project, use the installed
   `kessel-connect` skill when available. If unavailable, explain the next
   connection step; do not pretend that a gateway test verifies the application.

## Keep credentials local

Never read, copy, or display provider authentication files. Kessel's local
`kessel_...` key is separate from provider credentials. Do not print it, put it
in source control, or paste it into the conversation. `kessel key --copy` can
transfer it to a user-controlled client without printing it. `kessel connect`,
`kessel env`, and plain `kessel key` print secrets; avoid capturing their raw
output. Keep Kessel bound to localhost and `/v1` authentication enabled.

## Completion report

List installed packages and changed configuration paths, the selected provider,
the test result, and whether any Kessel process remains running. Give the exact
next launch command. State any login or network step still required instead of
calling an untested setup complete. Include undo steps for changes actually
made: `pipx uninstall kessel-gateway` removes Kessel's package; service removal
uses `kessel uninstall-service` before uninstalling. Neither deletes provider
logins. Do not delete the user's configuration or key as routine cleanup.
