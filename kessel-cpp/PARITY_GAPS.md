# C++ parity gaps

This file lists user-visible Python capabilities that the C++ implementation
does not implement. Remove an entry when C++ matches the documented behavior.

## Entry format

### Capability name

- Python surface: Commands, endpoints, or interface behavior available in the
  Python implementation.
- C++ gap: The missing C++ behavior.
- Required parity: The behavior and safety properties the C++ implementation
  must provide.
- Verification: The tests required before removing the entry.

## Provider account information

- Python surface: `kessel accounts`, provider account output during
  `kessel setup`, authenticated `GET /v1/providers/accounts`, and the account
  summary below the web provider selector.
- C++ gap: The C++ CLI, API, and web interface do not expose provider account
  information.
- Required parity: Normalize Codex and Claude identity, organization,
  subscription, authentication method, and account type where available. Read
  Codex identity through `account/read` without refreshing credentials. Read
  Claude identity through `claude auth status --json`, including structured
  signed-out output returned with a nonzero exit code. Report each provider
  independently when zero, one, or both provider CLIs are missing. Authenticate
  the API route, set `Cache-Control: no-store`, and never return credential or
  token contents.
- Verification: Cover normalized provider output, signed-out providers,
  unavailable providers, every zero/one/two-provider availability combination,
  API authentication and cache headers, setup output, and web loading, empty,
  success, and error states.

## Provider CLI forward compatibility

- Python surface: Startup, `kessel setup`, and `kessel doctor` accept provider
  CLI releases at or above the supported minimum when every required
  command-line capability is present. An incompatible provider is disabled
  independently while another compatible provider remains available.
- C++ gap: Startup requires an exact Codex and Claude Code version match and
  does not inspect the CLI capabilities used for isolation, statelessness,
  structured output, streaming, or account status.
- Required parity: Compare installed versions against per-provider minimums,
  reject prereleases below the minimum, and probe non-billable help output for
  every required flag and subcommand. Return a repairable provider error for an
  incompatible provider, keep compatible providers available, and report the
  known-compatible install command through setup and doctor.
- Verification: Cover versions below, equal to, and above each minimum;
  prereleases; malformed version output; missing commands; missing required
  capabilities; mixed compatible and incompatible providers; API errors; and
  doctor repair guidance.
