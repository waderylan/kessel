# Python build list

This list records the Python fixes and optimizations discovered while building
the separate C++ alternative. All ten items are implemented on `main`; the C++
implementation remains isolated on the `kessel-cpp-alternative` branch.

## Correctness fixes

- [x] **P-01 — Authenticate benchmark requests.** `benchmarks/run.py` accepts
  `--api-key` and otherwise loads `KESSEL_API_KEY` or the saved user config.
- [x] **P-02 — Make benchmark README updates effective.** The root README now
  contains the markers consumed by `update_readme_table()` and has a regression
  test for replacement behavior.
- [x] **P-03 — Reject ignored parallel tool requests.** OpenAI requests with
  `parallel_tool_calls=true` now return `400 unsupported_parameter` instead of
  silently running the single-tool path.
- [x] **P-04 — Validate Anthropic named tool choice.** A
  `tool_choice.type=tool` name must match the one supplied tool.
- [x] **P-05 — Validate `n` without coercion.** `n` now uses the same strict
  positive-integer type as token limits, rejecting booleans, strings, and
  floats instead of coercing them to `1`.
- [x] **P-06 — Honor provider command overrides in setup and doctor.** Health
  checks now use `KESSEL_CODEX_COMMAND`/`KESSEL_CLAUDE_COMMAND`, then saved
  command paths, then command-name defaults.

## Optimizations

- [x] **O-01 — Cache file-backed API-key parsing.** Authentication performs a
  stat-signature check and reparses only after the atomic config file changes.
  Key rotation remains effective on the next request and invalid configs fail
  closed.
- [x] **O-02 — Cache provider availability.** `/health` caches PATH resolution
  for five seconds and refreshes both providers concurrently.
- [x] **O-03 — Separate deterministic overhead benchmarks.** The live benchmark
  remains available, while `benchmarks/local_overhead.py` independently
  measures cached health, ASGI gateway, and process-launch overhead.
- [x] **O-04 — Initialize the tokenizer lazily.** `o200k_base` is loaded on the
  first token-controlled request and cached thereafter; requests without output
  limits avoid tokenizer initialization.

## Verification requirements

- Run the full Python test suite.
- Run the deterministic overhead benchmark.
- Confirm the existing API, CLI, provider, security, concurrency, output-control,
  structured-output, and lifecycle tests remain green.
- Obtain an independent review of the complete ten-item commit before pushing.
