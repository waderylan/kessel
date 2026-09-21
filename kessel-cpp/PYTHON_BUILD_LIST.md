# Python build list

Findings discovered while porting and testing Kessel in C++. This file is a
backlog only; the Python implementation was not modified.

## Bugs and correctness gaps

### P-01: benchmark requests omit authentication

- Severity: High for benchmark usability.
- Evidence: `benchmarks/run.py::run_once()` sends only `Content-Type`; every
  configured `/v1` route requires the Kessel API key.
- Impact: the documented benchmark fails with `401` against a normal service.
- C++ handling: the comparison harness reads an isolated benchmark key and
  sends `Authorization: Bearer ...` on every measured request.
- Python change: accept `--api-key` or load `UserConfig`, add the authorization
  header, and add a regression using an authenticated local fixture.

### P-02: benchmark README update silently does nothing

- Severity: Low.
- Evidence: `benchmarks/run.py::update_readme_table()` requires
  `<!-- benchmark-table:start -->` and `<!-- benchmark-table:end -->`; the
  current `README.md` has neither marker.
- Impact: a benchmark run updates result files but never updates the README,
  despite calling the update function.
- Python change: add the markers or remove the dead update path and document
  that `benchmarks/results.md` is canonical.

### P-03: `parallel_tool_calls=true` is accepted but ignored

- Severity: Medium protocol correctness.
- Evidence: `ChatCompletionRequest.parallel_tool_calls` is parsed, but neither
  the model validator nor `validate_chat_surface()` rejects `true`. The README
  states parallel selection is unsupported.
- Impact: clients can believe parallel tool behavior was honored when Kessel
  always permits at most one call.
- C++ handling: returns `400 unsupported_parameter` for a true value.
- Python change: reject `parallel_tool_calls=true` and add API/model tests.

### P-04: Anthropic named tool choice is not checked against the supplied tool

- Severity: Medium protocol correctness.
- Evidence: `AnthropicMessagesRequest.to_chat_request()` maps both `any` and
  `tool` to `required` but discards `tool_choice.name`.
- Impact: a request selecting a nonexistent tool can succeed by calling a
  different supplied tool.
- C++ handling: `tool_choice.type=tool` requires the selected name to match the
  single supplied tool.
- Python change: validate the name before conversion and add an Anthropic API
  regression.

### P-05: `n=true` is accepted as `n=1`

- Severity: Low validation correctness.
- Evidence: `n` uses a non-strict Pydantic `int` field, unlike the strict token
  limit fields. Python booleans are integer subclasses and Pydantic can coerce
  JSON `true` to `1`.
- Impact: a malformed request is silently accepted.
- C++ handling: requires a non-boolean positive JSON integer.
- Python change: use `PositiveStrictInt` (while retaining the `n=1` surface
  check) and add `true`, strings, and floats to validation tests.

### P-06: doctor/setup health checks ignore configured command overrides

- Severity: Medium operational correctness.
- Evidence: `check_providers()` hardcodes `codex` and `claude`; it does not read
  saved `codex_command`/`claude_command` or `KESSEL_*_COMMAND` overrides used by
  the running service.
- Impact: `kessel doctor` can report a provider missing while the configured
  service can execute it, or validate a different binary than the service uses.
- C++ handling: health checks use saved command paths and persist resolved
  native paths during setup.
- Python change: pass effective settings/commands into provider checks and add
  override regressions.

## Optimizations

### O-01: cache API-key JSON parsing by config modification time

- Current Python path: every authenticated request schedules
  `UserConfig.load()` through `asyncio.to_thread` when the key is file-backed.
- Cost: a thread-pool dispatch, file open, JSON parse, and validation per API
  request.
- C++ implementation: checks `last_write_time` and reparses only when the file
  changes, preserving next-request key rotation behavior.
- Python change: cache `(path, stat signature, key)` behind a lock; always fail
  closed on stat/read/parse errors; retain the immediate-rotation regression.

### O-02: cache provider executable availability for health checks

- Current Python path: `/health` calls `shutil.which` in worker threads for both
  providers on every request.
- Cost: two thread-pool jobs and repeated PATH/filesystem scans per probe.
- C++ implementation: caches both resolutions for five seconds, allowing new
  installations to appear without putting PATH scans on every health probe.
- Python change: resolve at startup and refresh on a short TTL or explicit
  configuration reload. Keep `/health` content redacted.

### O-03: separate gateway benchmarks from provider latency

- Current benchmark combines HTTP, process launch, provider startup, network,
  model inference, and quota variance.
- C++ implementation: `benchmarks/compare.ps1` uses the same deterministic
  native provider for both services and records distribution statistics.
- Python change: retain the live provider benchmark, but add deterministic
  gateway/process and health-only suites so framework changes are measurable.

### O-04: use an exact native tokenizer lazily

- Current Python path: `tiktoken` is exact but initializes at module import.
- C++ implementation: pinned C++ `o200k_base` BPE data initializes only when a
  request actually uses a token ceiling, keeping ordinary startup cheap.
- Python change: consider a lazy encoding accessor if startup profiling shows
  tokenizer import/initialization is material; preserve bit-exact tests.
