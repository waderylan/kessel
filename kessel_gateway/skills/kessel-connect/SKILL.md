---
name: kessel-connect
description: Connect an application or editor to Kessel, adapt existing API code, or create a working Python or Node example. Use when the user asks to use Kessel from a specific project or client.
---

# Connect a client to Kessel

Configure the requested application or tool and verify its actual request path.
This skill covers existing projects, minimal starters, and local API migration.

## Make the work visible

Inspect the target before editing. Report the detected SDK/API, selected
provider, base URL, startup mode, and exact files to change. Explain how the
local key reaches the client and that a verification request uses provider
quota. Announce meaningful changes in approach as evidence changes.

Follow the user's explicit instructions and existing authorization. A request
to connect an application authorizes its necessary configuration edits and
verification. Ask only about missing choices or additional scope; do not
silently install a login service, upgrade provider CLIs, or rewrite unrelated
application behavior. Installing this skill does not run it.

## Workflow

1. Inspect package manifests, SDK construction, configuration loading, and the
   normal launch command. Read only the relevant files and variable names;
   do not dump `.env` contents or existing credentials. For a new example,
   follow the requested language or ask if the project gives no indication.
2. Check `kessel --version` and `kessel doctor`. Read individual provider results:
   one working provider is sufficient even if doctor exits nonzero. If setup is
   missing and the user requested end-to-end onboarding, use `kessel-setup`
   when installed. Otherwise state the concrete setup requirement and ask only
   if performing it would expand the request. Do not claim an unready provider
   works. Use installed `--help` when commands differ from these instructions.
3. Identify the API surface before setting URLs. Read
   [API compatibility](references/api.md). Chat Completions can use either
   provider. Anthropic Messages requires Claude Code. Do not route an Anthropic
   client to Codex or claim every OpenAI API is implemented. Honor a requested
   provider. Otherwise use the sole ready compatible provider; if both fit and
   the project establishes no preference, ask which one to use. Do not silently
   substitute another provider when the requested one fails.
4. For a launchable application, prefer
   `kessel run --provider codex -- <normal application command>`; substitute
   `claude` when selected. Read [application examples](references/examples.md)
   only for the matching SDK. For an independently launched tool, follow
   [editor and service connections](references/clients.md).
5. Edit the smallest relevant configuration or client construction. Keep
   existing provider settings available through the application's existing
   configuration mechanism. Do not hard-code the Kessel key or override the
   user's global API credentials. Retain conversation history in the client;
   Kessel never resumes the previous request's provider conversation.
6. Run the application's smallest safe request through Kessel using harmless
   test input. Validate the feature the user needs: ordinary text, streaming,
   structured output, or the tool-call loop. Do not run a destructive tool or
   unrelated application task as a smoke test. A gateway setup test alone does
   not verify the application. For GUI-only clients, have the user perform the
   client-side test and report it as pending until they confirm it.

## Credentials and boundaries

Kessel's `kessel_...` key authenticates the local gateway; provider login
credentials stay with the provider CLI. Never read or copy provider auth files.
Avoid raw `kessel connect`, `kessel env`, or `kessel key` output in agent tools:
those print the local key. Prefer child environment injection via `kessel run`
or user-controlled clipboard transfer via `kessel key --copy`. Do not echo keys,
commit secrets, persist account identity, disable authentication, or expose the
gateway outside localhost. Skills run in the user's coding agent, not inside
Kessel's deliberately isolated provider requests.

## Completion report

List changed files, selected provider and API, the exact launch command or
editor settings locations, verification result, and remaining manual steps.
State what remains running. Give undo steps for the actual edits, and
`kessel uninstall-service` if a service was newly registered. Do not overwrite
unrelated changes with a blanket Git reset. Mark unsupported features or an
untested client explicitly rather than calling the integration complete.
