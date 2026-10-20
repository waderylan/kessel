# Independently launched editors and tools

Use a durable service when the client cannot be launched through `kessel run`.
Explain that `kessel start` also registers login startup. If the user only
requested a temporary session, offer `kessel run --provider codex` in a terminal
they keep open instead. A connection request alone does not authorize global
startup changes without that choice being established.

## Configuration

1. Identify the installed client's version and actual settings format.
   Inspect relevant non-secret settings or current official client docs before
   editing. Kessel's printed connection snippets are starting points, not proof
   of compatibility with every client release.
2. Report the provider, base URL, settings file or UI location, and startup mode.
   Preserve existing models and unrelated settings; merge a Kessel entry rather
   than replacing an entire file.
3. Use the OpenAI-compatible base URL `<root>/v1/codex` or `/v1/claude`, with
   model `default` for the initial test. Aider uses `OPENAI_API_BASE` rather than
   `OPENAI_BASE_URL`; adapt the launched child's environment without changing
   global credentials. Verify each client's actual variable handling.
4. Prefer `kessel key --copy` for the user to paste the local key into the
   client's secret setting. Never paste it into chat. If direct configuration
   is required and authorized, transfer it locally without emitting the value;
   use the client's supported secret store or an ignored private configuration.
   Ensure newly created secret files have private permissions. Do not place a
   key in a tracked settings file or a shared model definition.
5. `kessel connect TARGET --provider PROVIDER` supports Cursor, Continue, Aider,
   curl, and Python/Node SDK snippets. It prints the real key. Do not run it
   verbatim in an agent tool whose output goes into the conversation. Have the
   user run it locally if they want the generated settings.
6. Verify a harmless request from the actual client. Some client functions may
   use other API surfaces or run remotely. A local endpoint is usable only by
   code running on this machine; do not suggest tunnels or remote exposure as a
   workaround. State the exact client feature that was tested.

## Startup and undo

`kessel status` reports process availability. `/health` can return HTTP 200 and
still be degraded; check its provider status and the real client request.

`kessel stop` ends the process but retains its login registration.
`kessel uninstall-service` removes that registration while keeping config, key,
and logs. Removing the Kessel model entry or restoring only the settings edited
for this connection undoes the client configuration. Closing a foreground
owner stops attached applications; warn before stopping a session they use.
