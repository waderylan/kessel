# Kessel security and correctness audit

- Audit date: 2026-09-20
- Audited baseline: `e1d8e366416a5d437f8cf0feb37fba870ea90a95`
- Test host: Windows 10 build 26200, Python 3.10.19
- Provider versions: Codex CLI 0.155.1; Claude Code 2.1.278
- Remediation status: all confirmed actionable findings were fixed in the commit containing this report. One informational process-listing limitation remains documented in I-01.

## 1. Summary

- Overall assessment before remediation: Kessel's provider tool-isolation flags were effective in live attacks, but the HTTP trust boundary, secret handling, child-process environment, warm-process lifecycle, and resource bounds did not meet the stated security promises.
- Overall assessment after remediation: suitable for its stated single-user, localhost-only threat model, subject to the unverified platform and refresh-token items below.
- Severity counts:
  - Critical: 1
  - High: 4
  - Medium: 5
  - Low: 5
  - Informational: 1
- Validation completed after remediation:
  - `python -m pytest -q`: 122 passed.
  - `python -m compileall -q app tests`: passed.
  - `ruff check --select E9,F,B012,BLE001 app tests`: passed.
  - `uvx --from build pyproject-build`: sdist and wheel built successfully.
  - `uvx --from pip-audit pip-audit --path .venv/Lib/site-packages`: no known vulnerabilities found; only unpublished local distributions were skipped.
  - Live file-read/tool-use probes: fresh Codex, warm Codex, and fresh Claude refused or did not disclose a random external-file sentinel.
  - Live warm-server crash probe: request failed promptly, `close()` completed, descendants exited, and the private runtime directory was removed.
- Threat-model outcome:
  - Malicious web page: blocked by exact Host/port, Origin, JSON content-type, and API-key checks; DNS-rebinding-style headers were exercised directly.
  - Different local user: key/config creation is restrictive from the first write and errors expose no credentials; real second-account access tests remain unverified on this host.
  - Compromised same-user process: not a separable security boundary because it can directly read the user's Kessel and provider credentials; Kessel no longer forwards unrelated environment secrets.
  - Hostile valid-key client: can spend provider quota by design, but request/model/schema sizes, concurrency, duration, output, and process lifetime are bounded; no request content reaches a shell.
  - Prompt injection: live file/shell prompts failed across all three backends, and tools/config/customizations are disabled by CLI controls rather than prompt text alone.
  - Crash/disconnect/misbehaving CLI: process-tree, pipe-drain, output-bound, semaphore, restart, and cleanup paths passed targeted tests and the live warm crash probe.

## 2. Findings

### C-01 — API-key file was created with a permissive mode before chmod

- Severity: Critical
- Status: Fixed
- Affected code: baseline `app/user_config.py:53-62`; remediation `app/user_config.py:53-82`
- Description: `Path.write_text()` created the temporary configuration file using the process umask. On a normal POSIX `022` umask, the file was initially `0644`; only afterward was it changed to `0600`. Another local user could race the chmod and obtain the API key.
- Evidence and reproduction:

  ```sh
  git show e1d8e36:app/user_config.py | nl -ba | sed -n '53,62p'
  umask 022
  strace -e trace=openat,chmod python -c \
    'from app.user_config import UserConfig; UserConfig(api_key="kessel_test").save()'
  # Baseline ordering: open(..., 0666), then chmod(..., 0600).
  ```

- Impact: a cross-user credential disclosure during setup or any configuration rewrite. The stolen key permits authenticated use of the local provider APIs.
- Recommended fix: create a randomized, exclusive temporary file with mode `0600`, fsync it, and atomically replace the destination; make the directory `0700`.
- Resolution: implemented with `os.open(..., O_CREAT | O_EXCL, 0o600)`, `fsync`, atomic replace, randomized names, and restrictive directory/final modes. `tests/test_security.py::test_config_is_restrictive_and_atomic` covers the result.

### H-01 — DNS rebinding and cross-origin request guards were incomplete

- Severity: High
- Status: Fixed
- Affected code: baseline `app/main.py:103-126`; remediation `app/main.py:131-187`
- Description: CORS middleware validated `Origin` for browser-readable responses but did not validate `Host`. A DNS-rebound origin could address `127.0.0.1` while retaining an attacker-controlled host name. Requests without `Origin` and simple browser content types were also not rejected at the application boundary.
- Evidence and reproduction: an ASGI attack against the baseline returned `200` for `GET /health` with `Host: attacker.example:8000`; a request using the same hostile host and a valid key reached provider routing. A hostile-origin preflight returned `400`, showing CORS alone did not provide the missing Host control.

  ```powershell
  .venv\Scripts\python.exe -m pytest -q `
    tests/test_security.py::test_host_origin_and_simple_content_type_are_rejected
  ```

- Impact: a malicious web page could bypass the localhost hostname boundary through DNS rebinding. In deployments with missing authentication, H-02 made provider access possible.
- Recommended fix: require an exact loopback `Host` plus configured port, reject unapproved `Origin` values, and require `application/json` on state-changing API routes.
- Resolution: exact `127.0.0.1`, `localhost`, and `[::1]` host/port validation, Origin validation, and JSON media-type enforcement now run before routing.

### H-02 — Missing configuration disabled authentication

- Severity: High
- Status: Fixed
- Affected code: baseline `app/main.py:229-242`; remediation `app/main.py:290-320`, `app/cli.py:233-247`
- Description: `require_api_key()` returned successfully when no expected key was configured. `kessel serve` also allowed startup without a key.
- Evidence and reproduction: against the baseline, creating `Settings(api_key=None, ...)` made an unauthenticated `/v1/...` request reach provider routing rather than return `401` or `503`.

  ```powershell
  .venv\Scripts\python.exe -m pytest -q `
    tests/test_security.py::test_authentication_fails_closed_and_rotation_is_immediate
  ```

- Impact: configuration deletion, corruption, or partial setup converted an authenticated local service into an unauthenticated one. Combined with DNS rebinding, a hostile web origin could consume provider quota.
- Recommended fix: refuse service startup without a key and fail closed in the request dependency.
- Resolution: `kessel serve` refuses missing keys; the API returns `503` unless an explicit test-only setting enables unauthenticated operation.

### H-03 — Provider children inherited the full service environment

- Severity: High
- Status: Fixed
- Affected code: baseline `app/runner.py:268-280`, `app/providers/codex_app_server.py:92-108`; remediation `app/process_security.py:15-55`, `app/runner.py:265-300`, `app/providers/codex_app_server.py:95-120`
- Description: fresh and warm provider children used `os.environ.copy()`. This forwarded unrelated credentials, proxy configuration, API keys, and config overrides to binaries that did not need them.
- Evidence and reproduction: setting `KESSEL_AUDIT_SECRET=sentinel` in the parent caused a baseline test child to print the sentinel. The fixed regression proves it is absent while `PATH` and required provider overrides remain.

  ```powershell
  .venv\Scripts\python.exe -m pytest -q `
    tests/test_security.py::test_child_environment_scrubs_service_secrets
  ```

- Impact: a compromised, substituted, or unexpectedly instrumented provider binary could collect every secret in the service environment.
- Recommended fix: construct an allowlisted child environment containing only OS execution, home, locale, temporary-directory, and provider-specific values.
- Resolution: both process paths use `child_environment()`; request-specific Claude controls are narrow explicit overrides.

### H-04 — Provider stderr and internal errors were returned to clients

- Severity: High
- Status: Fixed
- Affected code: baseline `app/main.py:376-408`, `app/providers/codex_app_server.py:373-379`; remediation `app/main.py:456-491`, `app/providers/codex_app_server.py:386-390`
- Description: nonzero provider exits appended the last 1,000 stderr characters to HTTP errors, and warm startup errors embedded retained stderr. Provider diagnostics can contain filesystem paths, account details, CLI messages, or echoed data.
- Evidence and reproduction: a synthetic `ProcessExitError(stderr="SECRET-STDERR")` placed `SECRET-STDERR` in the baseline response body.

  ```powershell
  .venv\Scripts\python.exe -m pytest -q `
    tests/test_security.py::test_health_and_provider_errors_do_not_leak_details
  ```

- Impact: any API-key holder could extract local diagnostic data by provoking provider failures.
- Recommended fix: log only request metadata, retain stderr only for internal classification, and return stable generic errors.
- Resolution: client errors are mapped to generic messages; stderr is never included in normal or streaming error bodies.

### M-01 — Warm App Server crashes could hang requests and leave descendants or private state

- Severity: Medium
- Status: Fixed
- Affected code: baseline `app/providers/codex_app_server.py:517-552`, `app/runner.py:316-339`; remediation `app/process_security.py:188-258`, `app/providers/codex_app_server.py:536-615`, `app/runner.py:331-376`
- Description: on Windows, killing the warm App Server parent during a request left descendant processes holding files in the private `CODEX_HOME`. The request hung beyond ten seconds and cleanup raised a sharing violation on `goals_1.sqlite`.
- Evidence and reproduction: the pre-fix live crash probe produced a timeout and `PermissionError`. The same probe after remediation produced `{"outcome":"ProcessError","close":"clean","original_runtime_exists":false}`. Cancellation and process-group regressions are in `tests/test_concurrency.py` and `tests/test_runner.py`.
- Impact: denial of service, orphan processes, and retained private authentication/runtime files after crashes or disconnects.
- Recommended fix: place Windows children in kill-on-close Job Objects, retain a descendant fallback, reap processes, and shield/retry runtime cleanup.
- Resolution: implemented for fresh and warm paths, including spawn/initialization failure cleanup covered by `tests/test_codex_app_server.py::test_start_failure_removes_private_runtime`. POSIX continues to use a new session and `killpg`.

### M-02 — HTTP bodies and warm output queues were unbounded

- Severity: Medium
- Status: Fixed
- Affected code: baseline `app/main.py:95-126`, `app/providers/codex_app_server.py:202,417-425`; remediation `app/main.py:163-185`, `app/providers/codex_app_server.py:207,249-267,428-439`
- Description: the API had no request-body limit. Warm Codex used unbounded per-turn queues and did not apply the configured output limit to assembled text.
- Evidence and reproduction:

  ```powershell
  .venv\Scripts\python.exe -m pytest -q `
    tests/test_security.py::test_declared_and_chunked_oversized_bodies_are_rejected `
    tests/test_codex_app_server.py::test_warm_output_limit_interrupts_turn `
    tests/test_codex_app_server.py::test_warm_stderr_limit_fails_inflight_and_stops_process
  ```

- Impact: a hostile authenticated client or runaway provider could grow service memory until process or host failure.
- Recommended fix: bound declared and chunked request bodies, NDJSON lines, accumulated output, stderr, and per-turn queues.
- Resolution: bodies default to 1 MiB, provider bytes remain bounded, oversized NDJSON lines map to an output-limit error, warm text is byte-counted, and warm queues hold at most 1,024 events.

### M-03 — Key rotation did not take effect until restart

- Severity: Medium
- Status: Fixed
- Affected code: baseline `app/config.py:65-92`, `app/main.py:229-242`; remediation `app/config.py:65-118`, `app/main.py:290-320`, `app/cli.py:266-310`
- Description: the API key was loaded once into `Settings`. Replacing the key in `config.json` left the old key valid until service restart, and there was no atomic rotation command.
- Evidence and reproduction:

  ```powershell
  .venv\Scripts\python.exe -m pytest -q `
    tests/test_security.py::test_authentication_fails_closed_and_rotation_is_immediate `
    tests/test_cli.py::test_key_rotation_does_not_print_secret
  ```

- Impact: a revoked or suspected-compromised key retained access during the restart window.
- Recommended fix: atomically rotate the saved key and reload it for every request when no environment override is active.
- Resolution: `kessel key --rotate` writes atomically; the next request rejects the old key. Environment-provided keys cannot be silently rotated on disk.

### M-04 — Fresh Codex requests implicitly started the warm server

- Severity: Medium
- Status: Fixed
- Affected code: baseline `app/main.py:552-569,598,744`, `app/providers/codex.py:67-75`; remediation `app/main.py:675-681,826-830`
- Description: rate-limit preflight and response-header collection called warm-server methods even for `backend=fresh`. A nominally fresh request therefore created a persistent App Server and copied `auth.json` into a private runtime.
- Evidence and reproduction: on the baseline, a single fresh Codex request changed `CodexAppServer.is_running()` from false to true before provider completion. `tests/test_security.py::test_fresh_codex_does_not_invoke_warm_rate_limit_paths` asserts fresh requests do not invoke warm preflight or quota paths.
- Impact: violated backend selection, enlarged the process/authentication lifetime, and exposed fresh-only users to warm refresh-token behavior.
- Recommended fix: perform warm preflight and warm quota collection only for `backend=warm`.
- Resolution: fresh Codex no longer starts or queries the App Server.

### M-05 — Structured provider output was parsed but not validated against the client schema

- Severity: Medium
- Status: Fixed
- Affected code: baseline `app/models.py:38-70`, `app/structured.py:43-77`; remediation `app/models.py:14-93`, `app/structured.py:49-62`
- Description: Kessel accepted any parsed JSON as a successful `json_schema` response. Client schemas were not checked for validity, nesting limits, or potentially recursive references; returned data was not validated.
- Evidence and reproduction:

  ```powershell
  .venv\Scripts\python.exe -m pytest -q `
    tests/test_structured.py::test_malformed_or_referenced_schemas_are_rejected `
    tests/test_structured.py::test_structured_result_must_match_requested_schema
  ```

- Impact: downstream clients could trust a success response that violated their declared contract; hostile schemas could trigger remote resolution or excessive validator work if validation were later added naively.
- Recommended fix: validate schemas at request parsing, disallow references and excessive nesting, then validate provider payloads locally.
- Resolution: implemented with the pinned `jsonschema` validator.

### L-01 — Setup exposed the API key through terminal scrollback and the clipboard

- Severity: Low
- Status: Fixed
- Affected code: baseline `app/cli.py:192-221`; remediation `app/cli.py:189-225,266-310`
- Description: every setup printed the full key and automatically copied it when a clipboard utility existed.
- Evidence and reproduction: baseline `kessel setup` reached both `_copy_to_clipboard(config.api_key)` and `print(f"API key: {config.api_key}")`. The fixed test captures output and patches clipboard access.

  ```powershell
  .venv\Scripts\python.exe -m pytest -q tests/test_cli.py
  ```

- Impact: key retention in scrollback, clipboard history, remote terminal logs, or clipboard-manager synchronization.
- Recommended fix: do not reveal the key during setup; make printing and copying explicit actions.
- Resolution: setup prints URLs only. `kessel key` and `kessel key --copy` are deliberate user actions; rotation does not print the replacement.

### L-02 — Unauthenticated health output disclosed executable paths and versions

- Severity: Low
- Status: Fixed
- Affected code: baseline `app/main.py:428-447`; remediation `app/main.py:504-513`
- Description: `/health` returned configured command strings plus actual and tested versions. Absolute configured commands exposed usernames and installation layout.
- Evidence and reproduction: baseline `GET /health` returned `command`, `version`, and `tested_version` without authentication. The redaction regression checks that these strings are absent.
- Impact: useful local reconnaissance and unnecessary version disclosure to a malicious page that could reach the listener.
- Recommended fix: return only service and provider availability.
- Resolution: `/health` now returns only `status` and boolean `available` fields.

### L-03 — Generated service definitions mishandled special paths and unbounded launchd logs

- Severity: Low
- Status: Fixed
- Affected code: baseline `app/service.py:130-173`; remediation `app/service.py:128-214`
- Description: systemd `ExecStart` used shell quoting rather than systemd's parser rules, leaving `%` specifiers and `$` expansion unsafe. launchd wrote stdout/stderr to ever-growing files.
- Evidence and reproduction: a command argument containing `%n`, `$HOME`, quotes, and spaces was not escaped for systemd in the baseline. `tests/test_security.py::test_systemd_escaping_handles_special_path_characters` covers these cases.
- Impact: service startup corruption for unusual paths and disk exhaustion from persistent CLI diagnostics.
- Recommended fix: escape systemd `%`, `$`, backslash, and quotes; use plist argument arrays; avoid persistent provider log files when request logging already excludes content.
- Resolution: custom systemd escaping is used, service/plist files are mode `0600`, launchd output is sent to the null device, and all service types remain per-user/non-elevated. Windows startup uses the current user's `HKCU` Run entry rather than the administrator-only `schtasks /Create` path.

### L-04 — Version enforcement and dependency constraints allowed known-unsafe combinations

- Severity: Low
- Status: Fixed
- Affected code: baseline `app/config.py:90`, `pyproject.toml:1-29`; remediation `app/config.py:99`, `pyproject.toml:1-32`
- Description: environment-loaded settings disabled CLI-version enforcement by default despite the isolation contract depending on exact flags. Broad dependency ranges also retained `pytest 8.4.2` with CVE-2025-71176 and a development `setuptools 79.0.1` with PYSEC-2026-3447.
- Evidence and reproduction:

  ```powershell
  uvx --from pip-audit pip-audit --path .venv\Lib\site-packages
  # Before: pytest and setuptools advisories.
  # After: No known vulnerabilities found.
  .venv\Scripts\python.exe -m pytest -q tests/test_versioning.py
  ```

- Impact: a future unsupported CLI could silently ignore or change isolation flags; vulnerable development/build tooling remained installable.
- Recommended fix: enforce tested versions by default and pin reproducible runtime, development, and isolated-build versions.
- Resolution: exact pins are present, `setuptools 84.0.0` is required for isolated and development builds, and version enforcement defaults to true.

### L-05 — Streaming protocol events diverged from OpenAI and Anthropic shapes

- Severity: Low
- Status: Fixed
- Affected code: baseline `app/main.py:625-710,855-940`; remediation `app/main.py:703-792,952-1045`
- Description: OpenAI chunks omitted `usage: null` when `include_usage` was requested. Anthropic tool streams placed the complete input in `content_block_start` instead of using `input_json_delta`, and an error event could be followed by `message_stop`.
- Evidence and reproduction: response sequences were compared with the official [OpenAI Chat Completions reference](https://platform.openai.com/docs/api-reference/chat/create) and [Anthropic streaming Messages reference](https://platform.claude.com/docs/en/build-with-claude/streaming).

  ```powershell
  .venv\Scripts\python.exe -m pytest -q `
    tests/test_security.py::test_stream_protocol_fields_and_errors_are_safe
  ```

- Impact: strict SDKs or event consumers could reject streams or mis-handle tool arguments and terminal errors.
- Recommended fix: emit documented chunk fields and Anthropic event ordering.
- Resolution: fixed and covered by event-sequence assertions. Stop and finish reasons remain covered by `tests/test_output_controls.py`.

### I-01 — A small set of Codex controls remains visible in process listings

- Severity: Informational
- Status: Mitigated; accepted residual
- Affected code: baseline `app/providers/codex.py:109-134`, `app/providers/claude.py:69-91`; current `app/providers/codex.py:109-134`, `app/providers/claude.py:36-82`, `app/providers/registry.py:101-109`
- Description: baseline Claude commands placed model, effort, and full JSON schema text on argv. Claude now receives allowlisted model/effort values through its environment and schema text through stdin. Codex `exec` still requires its allowlisted model, reasoning-effort, and service-tier controls as documented CLI arguments; prompts, messages, schemas, tool definitions, and stop strings never use argv.
- Evidence and reproduction:

  ```powershell
  .venv\Scripts\python.exe -m pytest -q `
    tests/test_providers.py `
    tests/test_security.py::test_unapproved_model_is_rejected_before_provider_execution
  ```

- Impact: another local user who can inspect process arguments may learn a non-secret Codex model alias and selected enum values. Argument injection is blocked by exact model discovery/observation and enum validation.
- Recommended fix: if a future Codex release supports model/effort configuration through stdin, migrate these remaining controls to that transport.

## 3. Verified-safe areas

### Network exposure and browser attacks

- Bind enforcement: `UserConfig.load()` accepts only `127.0.0.1`, `localhost`, or `::1`; `kessel serve` has no host override flag and passes the validated host directly to Uvicorn (`app/user_config.py:106-121`, `app/cli.py:233-247`).
- DNS rebinding: exact loopback Host and configured-port checks pass the hostile-host regression (`app/main.py:135-150`).
- Browser requests: hostile Origins are rejected even on unauthenticated routes; JSON API POSTs reject simple content types; absent Origin does not bypass Host or API-key enforcement.
- CORS/preflight: only configured loopback origins, GET/POST, and the documented headers are allowed. Host/Origin guards execute independently of CORS.
- Route authentication: every `/v1` operation depends on `require_api_key`; `/`, static assets, docs, and redacted `/health` are intentionally public on the loopback listener.
- Error disclosure: tested health, normal error, and mid-stream error bodies contain no command path, CLI version, account text, or stderr.

### Authentication and secrets

- Key comparison: SHA-256 reduces both operands to fixed-length digests before `secrets.compare_digest` (`app/main.py:311-316`).
- Key generation: `kessel_` plus `secrets.token_urlsafe(32)` provides 256 random bits before encoding (`app/user_config.py:84-99`).
- File creation: POSIX config directories/files are created as `0700`/`0600` without a permissive creation window; Windows `icacls` inspection on the test host showed only SYSTEM, Administrators, and owner access for config and temporary runtime files.
- Rotation: the old saved key fails on the first request after atomic rotation. An explicit environment key remains authoritative and rotation is refused rather than misleading the operator.
- Secret propagation: API keys are absent from provider argv, allowlisted child environments, HTTP errors, structured logs, setup output, and default clipboard behavior.
- Logging: request logs contain request ID, method, path, status, and duration only; test logging and code review found no prompts, responses, Authorization header, or key.

### Process execution

- Prompt transport: all serialized conversations, schemas, tools, and content use stdin (`app/providers/base.py:34-48`, `app/providers/claude.py:86-120`, warm JSON-RPC stdin in `app/providers/codex_app_server.py`).
- Argument injection: request models must be provider aliases, previously observed exact IDs, or current exact Codex discovery results. Reasoning and service tier are Pydantic literals. Hostile model regression returns `400` before spawning.
- Child environment: only required OS, path, home, locale, temp, and explicit provider settings survive. Proxy variables and unrelated secrets are removed.
- Temporary directories: `tempfile.TemporaryDirectory` supplies unpredictable per-request paths; cleanup runs in `finally`. Warm cleanup is shielded/retried after the full process tree exits.
- Binary resolution: setup records absolute executable paths. On Windows it resolves the npm `codex.cmd` shim to Codex's packaged native executable so shell-free subprocess execution remains valid. Config host/path validation rejects malformed config; subprocesses always use argument arrays and no shell.

### Isolation and statelessness

- Codex flags: installed `codex exec --help` confirmed ephemeral execution, stdin prompt support, config/rules suppression, read-only sandbox selection, and feature/MCP controls used by the adapter. The instructions file is static and request-independent.
- Claude flags: installed `claude --help` confirmed `--no-session-persistence`, `--safe-mode`, `--restricted`, empty tools, and no permission prompts.
- Live hostile prompts: a random sentinel was written outside each request directory; prompts explicitly demanded file reads and command execution. Fresh Codex, warm Codex, and Claude did not return the sentinel. The sentinel was not found under the user's `.codex` or `.claude` data trees after the probes.
- Request persistence: fresh requests use new temporary working directories and ephemeral/no-persistence CLI modes. Normal, error, cancellation, and fixed crash paths remove the request runtime.
- Warm separation: every request issues `thread/start` with `ephemeral: true`; routing maps messages by thread and turn ID, and removes both mappings in `finally` (`app/providers/codex_app_server.py:172-311`). Concurrency routing tests use interleaved and out-of-order messages.
- Warm private home: it contains only the copied authentication file and generated isolation configuration, is not reused after server shutdown, and was deleted in normal and forced-crash probes.

### Concurrency and resource limits

- Async handlers: filesystem and CLI/version operations reachable from request paths use `asyncio.to_thread`; subprocess I/O uses asyncio APIs.
- Pipe draining: stdout and stderr are started and drained concurrently for fresh commands; warm stdout/stderr have independent reader tasks.
- Semaphore safety: provider slots use `async with`; timeout, provider exception, cancellation, and stream close release slots in `tests/test_concurrency.py`.
- Disconnect handling: stream finalizers close provider generators; fresh process groups are killed/reaped and warm turns receive `turn/interrupt`.
- Process groups: POSIX sessions use `killpg`; Windows uses kill-on-close Job Objects with a Toolhelp descendant fallback. Windows provider and cleanup processes also use `CREATE_NO_WINDOW`, so background requests do not allocate visible console windows. Cancellation tests verify no delayed marker is written by a surviving child.
- Warm routing/crashes: ID-specific futures and thread/turn queues prevent cross-request delivery. Reader failure atomically fails in-flight requests, clears state, and permits a clean restart.
- Bounds: request body, stdout/stderr, NDJSON lines, warm text, warm queues, message count, tool count, schema depth, concurrency, slot wait, request duration, and shutdown grace are bounded.
- Shutdown: new work is rejected, in-flight tasks receive the configured grace period, remaining work is cancelled, and fresh/warm process trees are reaped.

### Protocol correctness

- OpenAI: nonstreaming objects, streaming role/content/tool deltas, finish reasons, error objects, terminal usage chunk, and `[DONE]` framing are asserted in API and output-control tests.
- Anthropic: message start, content start/delta/stop, message delta, message stop, tool input JSON deltas, error objects, stop reasons, and request IDs are asserted against the official [Messages API](https://platform.claude.com/docs/en/api/messages) and [streaming sequence](https://platform.claude.com/docs/en/build-with-claude/streaming).
- UTF-8: subprocess decoding occurs only after complete byte lines/streams. `tests/test_runner.py::test_stream_preserves_utf8_split_across_pipe_reads` splits a three-byte character across writes.
- Stop handling: matches that straddle provider chunks are held back; the earliest occurrence wins; matched sequence text is never emitted. Tests cover full, boundary, terminal, competing, and unmatched sequences.
- Output truncation: token limits close the provider stream/process immediately, return `length`, and recompute delivered completion usage. Natural completion preserves provider usage.
- Structured/tool output: schemas are validated before execution; provider JSON must validate before a completion/tool call is emitted; malformed envelopes and unknown functions fail closed.
- Rate limits: known provider resets produce consistent `Retry-After`, percent-based quota headers, and rate-limit error types. Unknown limits omit rather than invent headers.

### Service and packaging

- Service privilege: Windows uses an `HKCU` Run entry and a detached current-user process; Linux uses `systemctl --user`; macOS installs a user LaunchAgent. No path requests elevation.
- Service files: Windows serializes the current-user startup command with `subprocess.list2cmdline`; launchd uses `ProgramArguments`; systemd special-character escaping has a regression test. Unit/plist files are `0600` on POSIX.
- Logs: application logs exclude content and credentials. Windows and launchd discard background standard streams; systemd journald owns Linux retention outside Kessel.
- Packages: clean isolated sdist/wheel builds succeeded. Wheel contents were limited to `app/`, static/provider assets, and distribution metadata. The sdist included source, tests, README, and build metadata but no `.env`, auth/config, local probe, or secret file. `app` matches the configured top-level package.
- Dependencies: runtime, dev, and build dependencies are exactly pinned; the post-fix advisory scan found no known vulnerabilities.
- Version gate: exact supported Codex/Claude versions are checked at startup by default and cannot be disabled accidentally by an absent environment variable.

### Setup and CLI

- Idempotency: `with_generated_key()` preserves an existing key; service installation updates one stable per-user identity without duplicates; setup tests cover repeated runs.
- Missing/malformed config: missing config uses validated defaults but service startup fails without a key. Invalid JSON, host, port, or field types raise a concise configuration error rather than starting insecurely.
- Doctor/status: code and captured-output review found no API key printing. Only explicit `kessel key` prints it; `--copy` avoids stdout.

## 4. Unverified

- Refresh-token rotation: the live warm probe confirmed the source `auth.json` was unchanged during ordinary requests, but a forced OAuth refresh could not be induced safely. It remains unverified whether Codex rotates refresh tokens in a way that invalidates or desynchronizes the user's main login when the private copy refreshes. Verification requires an expiring test account or an upstream guarantee about refresh-token reuse/rotation.
- POSIX/macOS runtime behavior: source-level modes, systemd escaping, launchd plist serialization, and per-user service selection were reviewed and unit tested on Windows, but real Linux/macOS installs were not executed. Verification requires disposable hosts on both platforms, `systemd-analyze --user verify`, `plutil -lint`, and cross-user file-access attempts.
- Cross-user process visibility: this host had no disposable second OS account. The argv review and allowlists establish what is exposed, but a second-account `ps`/Process Explorer test is still needed for each supported OS.
- Power-loss cleanup: process crash, parent kill, cancellation, disconnect, timeout, and graceful shutdown were exercised. Sudden machine power loss cannot run cleanup; OS temporary-file scavenging and the next setup/start behavior require a reboot/fault-injection environment.
- Forced long-duration quota/flood tests: concurrent local harnesses exercised slot exhaustion, interleaving, disconnects, oversized streams, and crashes without spending provider quota. A sustained real-provider flood and real quota exhaustion were not run to avoid account impact.
- Exact provider token accounting: response shapes and delivered-output truncation were checked. Exact Claude input-token values at the first SSE event and provider billing counters were not independently measured; this requires a provider billing fixture or authoritative CLI event capture.
- Dependency advisories: `pip-audit` was clean against its 2026-09-20 database. Future disclosures require routine rescanning; a point-in-time audit cannot prove future safety.
- Same-user compromise boundary: a process already running as the same user can ordinarily read that user's Kessel config and provider authentication files directly. No local service can make the API key a security boundary against that attacker without separate OS credentials or isolation. The audit verified that Kessel does not make this baseline access worse by placing unrelated secrets in provider children or responses.
