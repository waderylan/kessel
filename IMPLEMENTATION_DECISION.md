# Kessel Python vs. C++ Implementation Decision Report

- Review date: 2026-09-21
- Python candidate: `main` at `363ba68`
- C++ candidate: `kessel-cpp-alternative` at `de78917`
- Recommendation: ship Python, keep it as the long-term primary implementation, and retain C++ only as a performance/reference prototype.
- Review method: repository code, documentation, tests, build configuration, security audit, benchmark code, and committed benchmark results were reviewed. Clean temporary branch exports were used for independent builds, tests, benchmarks, package measurements, and behavior probes. The repository worktree was not used for generated test/build artifacts.

## 1. Executive recommendation

- Ship the Python implementation from `main`.
- Keep Python as the long-term primary implementation.
- Retain the C++ branch only as a performance/reference prototype until its useful optimizations and differential tests are transferred.
- Do not ship the current C++ artifact: it has a broken Windows distribution, a POSIX secret-file race, verified request-validation incompatibilities, and substantially weaker lifecycle/testing coverage.
- Decision drivers:
  - Python passed `156/156` current tests.
  - Python has the more complete API contract, security remediation, process lifecycle, observability, packaging path, and OS service support.
  - C++ is materially faster and smaller, but its advantages do not offset current correctness, security, distribution, and parity blockers.
  - Model-backed requests take roughly 1.9-5.2 seconds in the committed live results; the measured C++ gateway saving is tens of milliseconds, not faster inference.
- Confidence:
  - High for shipping Python now.
  - Medium for long-term performance projections because neither implementation has representative sustained-load or multi-platform benchmarks.

## 2. Side-by-side comparison

| Area | Python `main` | C++ alternative | Decision |
| --- | --- | --- | --- |
| Public HTTP routes | Full OpenAI/Anthropic routes, SSE, models, static UI, generated OpenAPI | Same nominal routes | Near parity |
| API documentation | FastAPI-generated schemas and validation | `/openapi.json` has empty operation objects; `/docs` is only a link (`kessel-cpp/src/server.cpp:279-280`) | Python |
| CLI commands | `setup`, `doctor`, `env`, `connect`, `key`, `start`, `stop`, `status`, `run`, `serve` | Same command names | Near parity |
| CLI validation | `argparse` rejects unknown/invalid flags | Hand-written parser silently accepts some invalid or unknown flags (`kessel-cpp/src/cli.cpp:300-306`) | Python |
| Protocol validation | Pydantic plus pinned `jsonschema` | Hand-written JSON parsing and partial schema validator | Python |
| Stateless fresh requests | Fresh provider process, isolated temporary directory | Equivalent fresh provider process | Tie |
| Warm Codex | New ephemeral thread per request; bounded queues; crash recovery | New ephemeral thread per request; thread-ID routing | Python on reliability |
| Provider isolation | Safe argv, environment allowlist, tools/config/rules disabled | Comparable controls in `kessel-cpp/src/process.cpp:22-54` and `kessel-cpp/src/providers.cpp:107-120` | Near parity |
| Local HTTP security | Host, Origin, content type, authentication, limits | Comparable HTTP boundary controls (`kessel-cpp/src/server.cpp:190-198`) | Near parity |
| Secret-file handling | Exclusive `0600` creation, fsync, atomic replace (`main:app/user_config.py:61`) | Creates with `ofstream`, then chmods (`kessel-cpp/src/core.cpp:137-175`) | Python |
| Partial-provider behavior | Codex-only, Claude-only, zero-provider, pre-setup tests | Same scenarios pass the PowerShell availability suite | Tie |
| Startup, 5-run mean | 1,495.84 ms | 536.26 ms | C++ |
| Idle working set | 48.97 MiB | 6.95 MiB | C++ |
| Deterministic request overhead | Higher | Lower | C++ |
| Distribution | `pipx`, Python 3.10+, wheel builds successfully | CMake install bundle currently misses a required DLL | Python |
| Installed footprint | 43.02 MiB site-packages, excluding Python | 9.398 MiB bundle, but incomplete | C++ after packaging fix |
| Cross-platform service support | Windows, systemd, launchd code | Windows and systemd; Linux-specific `/proc`; no launchd | Python |
| Test suite | 156 cases across API, security, lifecycle, concurrency, providers, benchmarks | Two CTest entries plus an unregistered live contract | Python |
| Observability | Request IDs, status, duration, cancellation logs | Request IDs but almost no request/lifecycle logging | Python |
| Development velocity | Modular adapters and established validators/frameworks | Manual HTTP, validation, SSE, OS, and lifecycle machinery | Python, by inference |

## 3. Python advantages and disadvantages

### Advantages

- Current test strength:
  - Command: `python -m pytest -q -p no:cacheprovider --basetemp <temp>`.
  - Result: `156 passed in 15.32s`.
  - Coverage includes:
    - availability and pre-setup: `main:tests/test_cli.py:105-229`;
    - security boundary: `main:tests/test_security.py:119`, `:409`, `:444`;
    - warm crash recovery: `main:tests/test_codex_app_server.py:145`;
    - shutdown/process reaping: `main:tests/test_concurrency.py:452`;
    - OpenAI/Anthropic protocol and output controls: `main:tests/test_api.py` and `main:tests/test_output_controls.py`.
- Stronger correctness:
  - Current `main` fixed all ten items listed in `main:PYTHON_BUILD_LIST.md`.
  - It now rejects `parallel_tool_calls=true`, validates named Anthropic tools, uses strict `n`, honors command overrides, authenticates benchmarks, and lazily initializes the tokenizer.
  - It delegates JSON Schema checking to `jsonschema`, rather than maintaining a partial validator.
- Stronger lifecycle behavior:
  - Explicit client-disconnect cancellation at `main:app/main.py:685`.
  - Graceful request draining and process reaping at `main:app/providers/registry.py:134-169`.
  - Warm Codex automatically restarts once after failure; covered at `main:tests/test_codex_app_server.py:145`.
- Stronger security history:
  - `AUDIT.md` documents fixes for DNS rebinding, fail-open authentication, environment-secret forwarding, stderr disclosure, unbounded queues, config permissions, and process cleanup.
  - The exact secret-file race still present in C++ was already classified Critical and fixed in Python: `AUDIT.md:37-55`.
- Operational completeness:
  - Structured request completion/cancellation logging includes request ID and duration: `main:app/main.py:290-353`.
  - Windows, systemd, and launchd service paths exist: `main:app/service.py:146`, `:203`, `:227`.
  - Clipboard handling supports Windows, macOS, Wayland, and X11: `main:app/cli.py:148-174`.

### Disadvantages

- Higher resource use:
  - Measured idle server working set: 48.97 MiB versus 6.95 MiB.
  - Measured cold readiness: 1.496 seconds versus 0.536 seconds.
- Larger runtime dependency graph:
  - Five direct runtime requirements in `main:pyproject.toml:11-17`.
  - A clean install resolved 31 application/runtime distributions and occupied 43.02 MiB across 3,348 site-package files.
  - Direct dependencies are exactly pinned, but transitives remain resolver-selected rather than locked with hashes.
- Installation prerequisites:
  - Requires Python 3.10+ and `pipx`.
  - Native dependencies such as `tiktoken`, `pydantic-core`, `httptools`, and `rpds-py` require compatible wheels or local compilation on less-common platforms.
- Platform verification remains incomplete:
  - `AUDIT.md:368-374` explicitly leaves real Linux/macOS execution, forced OAuth refresh behavior, exact billing-token accounting, and future dependency advisories unverified.

## 4. C++ advantages and disadvantages

### Advantages

- Lower startup and memory:
  - Five cold starts using mock provider commands and disabled version checks:
    - startup mean: 536.26 ms;
    - idle working set: 6.95 MiB;
    - private memory: 1.52 MiB.
- Lower gateway overhead:
  - Corrected comparisons against current Python `main` showed mean reductions of 23.1%, 36.5%, and 45.6% across three 100-request runs.
  - Cached `/health` over real sockets measured 0.271-0.365 ms/request versus Python's 2.459-2.825 ms/request.
- Smaller intended runtime:
  - Executable: 6,028,280 bytes.
  - Executable plus assets/tokenizer: 9,854,231 bytes.
  - No Python interpreter or Python package environment at runtime.
- Good core isolation design:
  - Fixed argv construction rather than shells: `kessel-cpp/src/process.cpp:143-170`, `:216-266`.
  - Environment allowlist: `kessel-cpp/src/process.cpp:22-54`.
  - Fresh Codex uses `--ephemeral`, ignores user config/rules, disables tools/features, and selects read-only sandboxing: `kessel-cpp/src/providers.cpp:107-120`.
  - Warm Codex creates an ephemeral thread per request: `kessel-cpp/src/providers.cpp:467-474`.
- Partial-provider handling:
  - Manual execution of `kessel-cpp/tests/cli-availability.ps1` passed Codex-only, Claude-only, zero-provider, and pre-setup scenarios.

### Disadvantages

- Windows install bundle is not runnable on a clean machine:
  - `objdump -p` reported `libwinpthread-1.dll`.
  - `cmake --install` did not copy that DLL.
  - Running the installed executable with MinGW removed from `PATH` exited `-1073741515`, Windows DLL-not-found status.
  - `kessel-cpp/CMakeLists.txt:71` statically links libgcc/libstdc++ but not winpthread.
  - This directly contradicts the "relocatable" claim in `kessel-cpp/README.md`.
- POSIX config secret race:
  - `kessel-cpp/src/core.cpp:145` creates the temporary config with `ofstream`.
  - `kessel-cpp/src/core.cpp:152` applies `0600` only afterward.
  - This is the same creation-window vulnerability fixed in Python's Critical C-01.
  - C++ also omits an fsync before replacement.
- Verified protocol-validation differences:
  - Paired mock-server probes produced:
    - Anthropic message role `system`: Python `400`, C++ `200`.
    - Numeric Anthropic `system`: Python `400`, C++ `200`.
    - OpenAI `tool_calls` as a string: Python `400`, C++ `200`.
  - Causes include permissive conversion in `kessel-cpp/src/core.cpp:540-583` and incomplete message-field validation in `kessel-cpp/src/core.cpp:475-485`.
- Warm-process reliability gaps:
  - Warm notifications are appended to an unbounded `std::deque`: `kessel-cpp/src/providers.cpp:294` and `:406-410`.
  - There is no automatic warm-server restart after process death.
  - A failed warm server remains stored and continues failing until Kessel restarts.
  - `shutdown_grace_seconds` is loaded at `kessel-cpp/src/core.cpp:268` but never used.
- Provider-authentication failure classification is effectively disconnected:
  - `classify_failure()` requires a nonempty provider to emit an authentication error: `kessel-cpp/src/process.cpp:57-74`.
  - Both calls omit the provider: `kessel-cpp/src/process.cpp:210` and `:266`.
  - Logged-out runtime failures therefore tend toward generic `502 provider_error`, not the documented actionable `503 provider_not_authenticated`.
- Weak cross-platform parity:
  - Non-Windows executable discovery assumes `/proc/self/exe`: `kessel-cpp/src/core.cpp:121`, `kessel-cpp/src/cli.cpp:25`.
  - Durable service installation assumes systemd: `kessel-cpp/src/cli.cpp:157`.
  - `key --copy` always invokes Windows `clip`: `kessel-cpp/src/cli.cpp:302`.
  - macOS launchd and `pbcopy` are absent.
- Sparse observability:
  - Request IDs are returned, but there are no normal request completion, latency, cancellation, or provider lifecycle logs.
  - Python records these without request content.
- Contributor complexity:
  - Major handlers and CLI flows are compressed into very long lines, notably `kessel-cpp/src/server.cpp:235-273` and `kessel-cpp/src/cli.cpp:300-378`.
  - Protocol, schema validation, process management, service installation, HTTP streaming, and CLI parsing are all maintained manually.

## 5. Feature and behavior gaps

### C++ gaps

- Full OpenAPI request/response schemas.
- Strict OpenAI and Anthropic field validation.
- Actionable logged-out-provider errors.
- Bounded warm queues.
- Warm-server crash recovery.
- Graceful shutdown deadlines and request draining.
- Non-streaming client-disconnect cancellation.
- Complete rate-limit/preflight headers and snapshots; C++ hard-codes `100/0` on detected exhaustion at `kessel-cpp/src/server.cpp:47-49`.
- macOS durable service and clipboard support.
- A complete Windows runtime bundle.
- Request lifecycle logging.

### Python gaps relative to C++

- Higher startup time and resident memory.
- Higher cached gateway overhead.
- Larger installed runtime.
- No standalone native bundle.

### Areas with effective parity

- Nominal public route set.
- OpenAI and Anthropic streaming shapes.
- Fresh Codex and Claude operation.
- Warm Codex with new ephemeral threads.
- Host/Origin/API-key boundary controls.
- Request/output limits.
- Safe argument arrays and child environment allowlists.
- Codex-only, Claude-only, zero-provider, and pre-setup workflows.

## 6. Benchmark results and methodology limitations

### Committed results

| Benchmark | Result | What it supports |
| --- | --- | --- |
| C++ branch comparison | Python 27.816 ms mean; C++ 17.225 ms; 38.1% reduction | C++ was faster in one mock/process-launch run |
| Python live Codex fresh | Total p50 3.318-4.062 s across efforts | Provider latency dominates gateway latency |
| Python live Codex warm | Total p50 2.359-2.814 s | Warm mode can reduce some Codex latency |
| Python live Claude | Total p50 1.861-1.996 s | Claude result only; not an implementation comparison |
| Python deterministic committed | Health 1.059 ms; ASGI gateway 1.802 ms; Python launch 72.883 ms | Python-only component costs |

### Independent results

- Current Python test suite:
  - `156 passed in 15.32s`.
- Current-main versus C++ deterministic comparison, three runs of 100:
  - Python means: `56.249`, `57.433`, `62.136` ms.
  - C++ means: `30.578`, `44.163`, `39.481` ms.
  - C++ reductions: `45.6%`, `23.1%`, `36.5%`.
- Python deterministic component benchmark, three runs of 100:
  - health ASGI mean: `1.678-1.839` ms;
  - gateway ASGI mean: `2.857-3.138` ms;
  - Python process launch mean: `138.019-150.502` ms.
- Cold startup and idle memory, five runs:
  - Python: `1,495.84 ms`, `48.97 MiB` working set.
  - C++: `536.26 ms`, `6.95 MiB` working set.
- Artifact/install measurements:
  - Python wheel: `68,498` bytes.
  - Python clean site-packages: `43.02 MiB`; interpreter excluded.
  - C++ executable: `6.028 MiB`.
  - C++ installed bundle: `9.398 MiB`; required winpthread DLL excluded.

### Commands and test outcomes

- Python branch export and suite:
  - `python -m pytest -q -p no:cacheprovider --basetemp <temp>`
  - Result: `156 passed in 15.32s`.
- C++ clean build:
  - `cmake -S <cpp-source> -B <temp-build> -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON`
  - `cmake --build <temp-build>`
  - Result: configuration and release build succeeded with CMake deprecation warnings in `cpp-tiktoken`/PCRE2.
- C++ CTest:
  - `ctest --test-dir <temp-build> --output-on-failure`
  - Result: self-test passed; CLI availability failed because the script hard-codes `<source>/build` as the permitted state root.
  - Manual invocation using the expected `<source>/build` state path passed the CLI availability suite.
- C++ comparison benchmark:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File kessel-cpp/benchmarks/compare.ps1 -Runs 100`
  - Repeated against both the branch-point Python snapshot and current Python `main`.
- Python deterministic benchmark:
  - `python benchmarks/local_overhead.py --runs 100 --output-dir <temp>`
  - Repeated three times.
- Packaging:
  - `python -m pip wheel <main-export> --no-deps --no-build-isolation`
  - Clean virtual environment installation completed in 31.42 seconds.
  - `cmake --install <temp-build> --prefix <temp-install>` succeeded but omitted `libwinpthread-1.dll`.
- Windows runtime dependency verification:
  - `objdump -p kessel-cpp.exe` listed `libwinpthread-1.dll`.
  - Running with a PATH limited to Windows system directories returned `-1073741515`.

### Methodology limitations

- The committed C++ comparison uses the older Python snapshot on the alternative branch, not current `main`.
- The corrected comparison still measures one Windows host, sequential requests, a trivial native mock, PowerShell client overhead, and fresh process creation.
- The 21-52% observed reduction is too variable to advertise as a precise figure.
- The C++ benchmark does not measure model inference, real provider networking, concurrency, throughput saturation, startup, RSS, or long-run reliability.
- The committed live benchmark has only three samples per case; its "p95" is effectively the maximum sample.
- Live results include provider load, subscription state, network variability, and model behavior.
- The Python ASGI benchmark is in-process and does not include TCP, Uvicorn, or client overhead.
- Wheel-versus-bundle size is not directly equivalent: the Python wheel excludes dependencies and Python; the C++ bundle currently excludes a required DLL.
- A C++ live contract run passed the Codex portion but failed at `kessel-cpp/tests/contract.ps1:46` on the Anthropic request. Direct Claude execution returned a provider-side HTTP 500, so this run is inconclusive for C++ correctness and leaves full live Claude parity unverified.
- Unsupported claims:
  - C++ does not make models infer faster.
  - C++ is not inherently safer because it is compiled.
  - The current 38.1% number does not represent production end-to-end speed.
  - Python's source-level macOS/Linux support does not prove successful real installations there.

## 7. Shipping risks ranked by severity

| Severity | Risk |
| --- | --- |
| Critical | C++ Windows install omits `libwinpthread-1.dll` and fails on a clean runtime path. |
| Critical on POSIX | C++ recreates the config/API-key permissive-creation window previously rated Critical in Python's audit. |
| High | C++ accepts malformed OpenAI/Anthropic requests that Python rejects, creating protocol divergence. |
| High | C++ warm notification queues are unbounded and warm server failures do not recover automatically. |
| High | C++ contract coverage is not part of CTest; major HTTP/security/lifecycle paths lack automated regressions. |
| High | Maintaining both implementations has already caused drift: stale Python findings, an outdated benchmark baseline, and divergent validation behavior. |
| Medium | C++ logged-out provider errors do not follow the documented actionable failure contract. |
| Medium | C++ has no real macOS service path and Linux/macOS runtime code was not exercised. |
| Medium | C++ lacks graceful shutdown, disconnect handling, and useful request telemetry. |
| Medium | `ctest` fails for valid arbitrary out-of-source builds because `kessel-cpp/tests/cli-availability.ps1:10-13` assumes `<source>/build`. |
| Medium | Neither dependency strategy is hermetic: Python leaves transitives unlocked; C++ uses mutable tags for two FetchContent dependencies and downloads source during configuration. |
| Low | Python consumes more memory and starts more slowly. |
| Low | Python's installation surface is larger and depends on Python/pipx availability. |

## 8. Conditions under which Python should be selected

- Shipping now is required.
- OpenAI/Anthropic compatibility and strict failure behavior matter more than tens of milliseconds.
- Windows, Linux, and macOS are intended targets.
- `pipx` and Python 3.10+ are acceptable prerequisites.
- Future work will add routes, request controls, providers, schemas, tools, telemetry, or service behavior.
- Contributor accessibility and debugging speed matter.
- A single production implementation is preferred.
- Provider calls normally take hundreds of milliseconds to seconds.

## 9. Conditions under which C++ should be selected

- A native, low-RSS, fast-starting local gateway is a measured product requirement.
- Workloads contain many short/local provider calls where gateway overhead is material.
- The team can own C++ HTTP, process, OS-service, schema, and packaging code across supported platforms.
- Selection occurs only after these gates:
  - bundle or statically link winpthread;
  - use exclusive restrictive config/runtime creation on POSIX;
  - run one shared differential API corpus against both implementations;
  - bound warm queues and implement crash recovery;
  - implement graceful shutdown and disconnect cancellation;
  - add request telemetry;
  - provide and test Windows, Linux, and macOS release artifacts;
  - register deterministic HTTP contract tests in CTest;
  - rerun an independent security review.

## 10. Final recommendation

- Shipping now: Python `main`.
- Long-term primary: Python, unless measured production data later shows startup/RSS or local high-rate request overhead is a dominant user problem.
- Retain C++: yes, as a non-production reference branch for:
  - startup and memory targets;
  - native tokenizer integration;
  - executable-resolution ideas;
  - deterministic cross-implementation benchmarking.
- Do not maintain two production implementations:
  - Use the C++ branch to inform Python optimization.
  - Archive it if no funded native-distribution requirement emerges.
- Immediate release gate for Python:
  - Update documentation to cite the current `156`-test result rather than historical `122/133` counts.
  - Use the already passing wheel and test outputs for the release record.

## 11. Test quality and coverage detail

- Python test inventory:
  - 16 test files.
  - 2,888 lines of test code.
  - 118 explicit test functions.
  - 156 collected cases after parametrization.
- C++ test inventory:
  - 10 checks in the built-in `self-test`.
  - 29 `Assert-True` calls in `kessel-cpp/tests/cli-availability.ps1`.
  - 25 `Assert-True` calls in `kessel-cpp/tests/contract.ps1`.
  - Only the self-test and Windows CLI availability script are registered in `kessel-cpp/CMakeLists.txt:99-113`.
  - The HTTP contract requires an independently started server and real logged-in providers; it is not part of normal CTest execution.
- Python test strengths:
  - Security boundary and failure redaction.
  - API-key rotation and fail-closed behavior.
  - Process-group cleanup and child reaping.
  - Disconnect handling and shutdown cancellation.
  - Warm-server failure and restart.
  - Concurrent warm-request isolation.
  - Stream event ordering and error shapes.
  - Token truncation, stop sequences, structured output, and schema enforcement.
  - Partial provider availability and pre-setup behavior.
- C++ test strengths:
  - Core token/schema/tool checks.
  - CLI partial-provider scenarios.
  - Live nominal API routes, stream terminators, stop sequences, and concurrent warm requests.
- C++ test gaps:
  - No registered deterministic HTTP contract.
  - No config-permission race test.
  - No bounded warm-queue test.
  - No warm crash/restart test.
  - No child-environment regression.
  - No process-tree cleanup test.
  - No disconnect or graceful-shutdown test.
  - No differential request-validation corpus.
  - No packaged-artifact launch test.

## 12. Dependency and supply-chain detail

- Python:
  - Direct runtime dependencies are pinned exactly in `main:pyproject.toml`:
    - `fastapi==0.141.1`;
    - `jsonschema==4.25.1`;
    - `pydantic==2.13.5`;
    - `tiktoken==0.14.0`;
    - `uvicorn[standard]==0.53.0`.
  - Build backend requirement pins `setuptools==84.0.0`.
  - Transitive dependencies are not lockfile- or hash-pinned.
  - A fresh install selected 31 application/runtime distributions.
  - `AUDIT.md` records a clean `pip-audit` result as of 2026-09-20, not a continuing guarantee.
- C++:
  - `kessel-cpp/CMakeLists.txt` fetches:
    - cpp-httplib tag `v0.56.0`;
    - nlohmann/json tag `v3.12.0`;
    - cpp-tiktoken commit `9323db528d52e48900c75ce197c3251085b18480`;
    - cpp-tiktoken's pinned PCRE2 submodule.
  - Tags can be moved upstream; no downloaded-source hash verification is configured.
  - A clean build requires CMake 3.25+, a C++20 compiler, Git/network access for FetchContent, and a platform build tool such as Ninja.
  - CMake emitted deprecation warnings from cpp-tiktoken and PCRE2 during the clean build.
  - Runtime dependency count is smaller, but the current Windows bundle omits one required runtime DLL.

## 13. Operational reliability and observability detail

- Python:
  - Tracks active provider tasks and drains or cancels them during shutdown.
  - Watches client disconnects for buffered and streaming requests.
  - Closes provider streams immediately when stop/token controls terminate output.
  - Restarts a failed warm Codex App Server once.
  - Emits structured request lifecycle records with request ID, method/path, status, and duration.
  - Does not log prompts, responses, credentials, or provider stderr to clients.
- C++:
  - Uses provider slot pools and an eight-thread HTTP pool.
  - Fresh provider process groups are terminated on timeouts or stream callback termination.
  - Warm requests route events by thread ID.
  - Does not explicitly drain active requests according to `shutdown_grace_seconds`.
  - Does not restart a failed warm App Server.
  - Does not bound the warm notification queue.
  - Does not emit normal request completion or latency records.
  - Redacts provider details from clients, but this also made the failed live Claude contract return only `Provider request failed`.

## 14. Development velocity and duplication risk

- Verified facts:
  - The alternative branch contains the older Python snapshot plus the new C++ implementation.
  - Current `main` later fixed ten Python findings and added a deterministic Python overhead benchmark.
  - The committed C++ comparison still targets the older branch-point Python code.
  - Request validation already differs between the two implementations.
- Inference:
  - Python should deliver future API and provider features faster because it relies on FastAPI, Pydantic, `jsonschema`, asyncio, and modular provider adapters.
  - C++ requires every new field or behavior to be implemented across manual JSON parsing, validation, HTTP response construction, SSE, provider invocation, and tests.
  - Treating both implementations as production would approximately double contract, security, platform, release, and provider-version maintenance while still risking drift.
- Action:
  - Keep a single authoritative production implementation.
  - Use cross-implementation benchmarks and C++ design work as evidence for targeted Python optimization.
  - Do not promise feature parity for the C++ branch unless a shared differential conformance suite becomes a release gate.
