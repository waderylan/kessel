# Kessel C++

Modern C++20 implementation of Kessel. It preserves the Python service's public
commands and OpenAI/Anthropic HTTP shapes while remaining isolated in this
directory. It does not import, modify, or replace the Python package.

## Implemented surface

- Commands: `setup`, `doctor`, `env`, `connect`, `key`, `start`, `stop`,
  `status`, `run`, and `serve`.
- Routes: `/health`, provider model lists, OpenAI Chat Completions, Anthropic
  Messages, SSE streaming, `/`, static assets, `/docs`, and `/openapi.json`.
- Providers: fresh Codex, fresh Claude, and persistent warm Codex. Every warm
  request creates a new ephemeral thread; concurrent turns are routed by
  thread ID.
- Output controls: exact `o200k_base` token ceilings, stop sequences, JSON
  schemas, one function tool, usage, and finish reasons.
- Security: localhost Host/Origin checks, API-key authentication and rotation,
  request/output limits, fixed argument arrays, provider environment allowlists,
  read-only Codex sandboxes, and child-process group cleanup.

## Build

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
cmake --install build --prefix dist
```

The executable is `build/bin/kessel-cpp.exe` on Windows. The install command
creates a relocatable `dist/bin` containing the executable, web assets,
instructions, and tokenizer data.

CMake fetches pinned revisions of cpp-httplib, nlohmann/json, and cpp-tiktoken.
See `THIRD_PARTY_NOTICES.md` for versions and licenses.

## Commands

```text
kessel-cpp setup
kessel-cpp doctor
kessel-cpp env --provider codex --shell powershell
kessel-cpp connect openai-python
kessel-cpp key [--copy|--rotate]
kessel-cpp start
kessel-cpp stop
kessel-cpp status
kessel-cpp run --provider codex -- <application command>
kessel-cpp serve
```

One installed and logged-in provider is enough. Setup configures and tests the
available provider. If neither provider is ready, setup exits without creating
an API key and prints the install or login commands needed to continue.

Configuration is compatible with Python Kessel's `config.json`. For isolated
testing, set `KESSEL_CONFIG_DIR` and `KESSEL_STATE_DIR` to directories under
this project.

## Compatibility and benchmarks

`tests/contract.ps1` exercises health, authentication, OpenAI completion,
Anthropic Messages, streaming, validation, output controls, and static routes.
`benchmarks/compare.ps1` measures equivalent Python and C++ HTTP overhead using
mock provider executables, separating gateway cost from provider network time.

```powershell
# Requires logged-in Codex and Claude installations.
powershell -NoProfile -ExecutionPolicy Bypass -File tests/run-live-contract.ps1

# Runs both services with the same deterministic native provider.
powershell -NoProfile -ExecutionPolicy Bypass -File benchmarks/compare.ps1 -Runs 100
```

The latest 100-request run measured:

| Service | Mean | p50 | p95 |
| --- | ---: | ---: | ---: |
| Python | 27.816 ms | 27.551 ms | 29.835 ms |
| C++ | 17.225 ms | 16.948 ms | 18.645 ms |

The C++ gateway reduced mean latency by **38.1%** for this process-launch-heavy
mock workload. These numbers isolate gateway/process overhead and do not claim
that model inference is faster by the same percentage. Machine-readable results are in
`benchmark-results/results.json`.

Python bugs and candidate backports found during the port are tracked in
`PYTHON_BUILD_LIST.md`. No Python source was changed.

## Verification scope

- Windows 11/MinGW: release build, native self-tests, live Codex/Claude HTTP
  contract, concurrent warm-turn isolation, and benchmark verified.
- Python regression baseline: 133 tests passed with bytecode and pytest cache
  redirected into this directory.
- POSIX process/service paths are implemented but were not runtime-tested on
  this Windows host.
