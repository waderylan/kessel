# Kessel repository instructions

## Project shape

- Kessel is a Python 3.10+ localhost gateway in `kessel_gateway/`.
- The PyPI distribution is `kessel-gateway`; the import package is
  `kessel_gateway`; the public executable is always `kessel`.
- Keep `kessel_gateway.__version__` as the single version source.
- `cli.py` owns user commands, `main.py` owns FastAPI routes, and `models.py`
  owns public request and response schemas.
- Provider commands, capability checks, and response parsing belong in
  `kessel_gateway/providers/`. Shared subprocess behavior belongs in
  `runner.py` and `process_security.py`.
- User configuration and service management belong in `user_config.py` and
  `service.py`. Keep the browser client dependency-free in `static/`.

## Required behavior

- Keep requests stateless. Fresh provider processes are the default; warm
  backends must create a new ephemeral thread for every request and never
  resume prior request state.
- Preserve the documented OpenAI Chat Completions and Anthropic Messages
  request, response, streaming, and error shapes.
- Bind to `127.0.0.1` by default and authenticate every `/v1` route.
- Never read, copy, log, return, or persist provider credentials or provider
  authentication files. Account identity is read on demand and not retained.
- Use subprocess argument arrays with `shell=False`. Never interpolate user
  input into commands, and keep the child environment allowlisted.
- Do not bypass provider authentication, quotas, safeguards, or supported CLI
  behavior. Preserve the boundaries documented in `PROVIDER_TERMS.md`.
- Accept newer provider versions only when capability probes pass. Keep minimum
  and known-stable versions centralized in `providers/compatibility.py`.
- Keep the frontend accessible and cover loading, empty, success, and error
  states.

## Extending Kessel

- For provider changes, update the adapter, registry, health and compatibility
  checks, account normalization, and provider-focused tests together.
- For API changes, update Pydantic models first, retain authentication and host
  validation, then add contract and streaming tests.
- For prompt, structured-output, or tool-call changes, test both providers and
  failure paths; do not silently weaken requested constraints.
- For configuration changes, preserve existing config files, private file
  permissions, and safe defaults across Windows, macOS, and Linux.
- Keep user documentation in the present tense and update `README.md` whenever
  commands, installation, configuration, or public behavior changes.
- Mock provider processes in automated tests. Live CLI checks are optional
  release smoke tests and must not be required for the unit suite.

## Validation

- Run `python -m pytest -q` for every behavior change.
- Run `python -m compileall -q kessel_gateway tests benchmarks` after package
  moves or import changes.
- Run `node --check kessel_gateway/static/app.js` after frontend JavaScript
  changes.
- For release changes, build both wheel and source distribution, run
  `twine check`, inspect their contents, and install the wheel in a clean
  virtual environment before publishing.

## Git

- Preserve unrelated working-tree changes.
- Use a concise commit subject followed by a body containing 1-4 `-` bullets.
- Do not add AI-tool co-author or contributor trailers.
