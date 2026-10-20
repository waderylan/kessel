# Diagnose the failing layer

Read the matching row, then test the hypothesis. Do not apply every repair.

| Symptom | Evidence to inspect | Scoped repair |
| --- | --- | --- |
| `kessel` command missing | pipx installation and executable location, current shell PATH | Restore executable visibility; install only if absent |
| Provider missing | Selected provider's `kessel doctor` entry and executable resolution | Fix PATH or saved executable path; install selected provider only if missing |
| Incompatible provider | Doctor's version/capability failure and known-stable command | Use its recovery command when replacement is authorized; retain checks |
| Provider appears signed out | Doctor result plus supported CLI login-status command | Distinguish timeout/process failure from authentication; user completes official login if needed |
| Setup missing | CLI says to run setup | Run `kessel setup` when repair scope permits; do not hand-edit a key |
| Connection refused | `kessel status`, configured port, startup mode | Start the intended mode; setup itself leaves no server running |
| Wrong process on port | Kessel port-conflict message and process ownership | Select another port with `kessel setup --port PORT`, then update affected clients; do not kill an unrelated process |
| HTTP 200 but no provider works | `/health` JSON `status`, `message`, and `providers` | Resolve degraded provider availability; HTTP 200 alone proves only process reachability |
| 401/authentication error | Client auth header method, stale local key, presence of `KESSEL_API_KEY` override | Reconnect with the effective Kessel key through local transfer; do not print values or rotate routinely |
| 404/wrong route | SDK method and configured base URL | Use Chat Completions base `<root>/v1/{provider}` or Anthropic base `<root>`; Responses API is unsupported |
| Request validation error | Field names and error category, installed `/docs` schema | Correct the incompatible field while preserving requested constraints; explain unsupported input |
| 429/busy or quota error | Kessel error code/message and retry guidance | Distinguish gateway concurrency from provider quota; bound retries and honor retry timing; never switch accounts to evade limits |
| Timeout/proxy/TLS failure | CLI status versus server environment; selected proxy/CA variable names | Correct supported proxy/CA configuration; do not disable TLS checks |
| First token-limited request fails offline | Setup token-counter warning | Restore access for the token-data download; do not drop requested token limits |
| Works in terminal, fails as service | Service-captured PATH/proxy/CA settings and saved executable location | Refresh service environment as described in maintenance |
| Tool calls appear but no tool runs | Application's tool execution and follow-up messages | Client executes the function and returns each result with its call ID; provider tools stay disabled |
| Conversation forgets prior turns | Outbound message history structure | Include history on every request; Kessel never resumes request state |

## Safe diagnostic collection

- Prefer CLI output and selected fields over raw files or broad environment
  dumps. The config file includes a key; `.env` may include other credentials.
- `kessel status` reports the configured URL. A URL hard-coded to port 4880 is
  wrong when setup selected another port.
- `KESSEL_API_KEY` overrides the saved key. Check whether it is set without
  printing it. Rotation fails while that override is active, and a rotated saved
  key will not fix a client using a different override.
- Use `kessel key --copy` for a user to update a client's secret field. Only
  rotate a key when requested or explicitly authorized for a concrete reason;
  every existing client will need the replacement.
- Model discovery requires local authentication at
  `GET /v1/{provider}/models`; `/health` intentionally contains no account info.
- Fresh requests start new provider processes. Codex warm operation still uses
  a new ephemeral thread per request. Do not enable session persistence as a
  repair for missing conversation history.

For a support report, include Kessel/provider versions, OS, startup mode, route,
error code, and redacted relevant log lines. Exclude keys, authentication files,
account identity, user prompts, and responses. Do not publish or send a report
unless the user explicitly requests it.
