# Kessel

Kessel is a local HTTP API for your existing Codex and Claude Code logins. It lets software that speaks the OpenAI Chat Completions API or Anthropic Messages API use the provider CLIs already authenticated on your computer.

```text
Your application -> Kessel on 127.0.0.1 -> Codex or Claude Code CLI
```

Kessel is intended for local development. It does not expose your subscription to remote deployments, and it is not a replacement for a production OpenAI or Anthropic API account.

## Install and configure

### 1. Install and log in to at least one provider

| Provider | Install | Log in |
| --- | --- | --- |
| Codex | `npm install -g @openai/codex` | `codex login` |
| Claude Code | `npm install -g @anthropic-ai/claude-code` | `claude login` |

Kessel requires Codex `0.155.1` or later or Claude Code `2.1.278` or later.
Newer releases are accepted when their command-line help confirms every
isolation, statelessness, structured-output, streaming, and account-status
capability Kessel uses. `kessel doctor` reports an incompatible provider and
the update command without preventing another compatible provider from running.
Known-stable recovery versions are Codex `0.155.1` and Claude Code `2.1.280`.

Kessel also requires Python 3.10 or later and [pipx](https://pipx.pypa.io/).

### 2. Install Kessel

```text
pipx install kessel-gateway
kessel setup
```

`kessel setup` performs five tasks:

- Checks that provider CLIs are installed and logged in.
- Displays the account identity and plan reported by each ready provider.
- Creates a private local API key beginning with `kessel_`.
- Starts a temporary Kessel process and sends a test request through each available provider.
- Stops that temporary process when testing finishes.

Setup does not install or leave a background service running. It is safe to run again and preserves the existing Kessel key.
One working provider is enough. If neither provider is installed and logged in,
setup exits without creating a configuration and explains how to continue.

### 3. Choose how Kessel should run

Kessel has three startup modes. The API behavior is identical in every mode; only process ownership and shutdown behavior differ.

| Mode | Command | Use it when | Kessel stops when |
| --- | --- | --- | --- |
| App-managed | `kessel run --provider codex -- python app.py` | One application needs Kessel | Its owned temporary Kessel stops when the application exits or the terminal closes |
| Durable | `kessel start` | Multiple terminals, tools, or projects need Kessel | You run `kessel stop` |
| Foreground owner | `kessel run --provider codex` | You want Kessel available before the application exists | You press Ctrl+C or close the owner terminal |

### Option 1: App-managed Kessel — recommended

Start Kessel and your application together:

```text
kessel run --provider codex -- python app.py
```

Replace `codex` with `claude` to make the OpenAI SDK route use Claude Code:

```text
kessel run --provider claude -- python app.py
```

This command:

1. Starts a temporary Kessel server if one is not already running.
2. Supplies the Kessel URL and key to `app.py` through its environment.
3. Runs the application with its normal terminal input and output.
4. Stops the temporary Kessel server when the application exits.

The application runs directly in Kessel's own terminal session, so Ctrl+C, `/dev/tty` access, and other terminal signals reach it exactly as if you had launched it yourself; Kessel steps out of the way of SIGINT while the application runs. Closing the terminal (or sending SIGHUP or SIGTERM to Kessel) stops the managed application and the temporary Kessel server together, on Windows, macOS, and Linux alike. Kessel never changes your global or parent-terminal environment.

The application reads the standard variables expected by its SDK:

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url=os.environ["OPENAI_BASE_URL"],
    api_key=os.environ["OPENAI_API_KEY"],
)

response = client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "Hello"}],
)

print(response.choices[0].message.content)
```

The value in `OPENAI_API_KEY` is Kessel's local `kessel_...` key. The variable has that name because the OpenAI SDK requires it; it is not an OpenAI-issued credential.

### Option 2: Durable Kessel

Start a per-user service that remains available after the current terminal closes:

```text
kessel start
```

Use the same `kessel run` wrapper to launch applications with the correct URL and key:

```text
kessel run --provider codex -- python app.py
```

`kessel run` will print `Durable Kessel detected`, reuse the existing service, and leave it running when the application exits.

Stop the durable service explicitly:

```text
kessel stop
```

`kessel start` registers Kessel as a login-scoped service for the current operating-system user. It can start again at the next login after `kessel stop`; the stop command ends the current service process without deleting its registration, configuration, or API key. Run `kessel uninstall-service` to remove the registration itself (the Windows startup entry, the macOS launch agent, or the systemd user unit); it leaves your configuration, API key, and logs in place and is safe to run again.

At install time, `kessel start` captures your current `PATH` (so an npm-installed `codex`/`claude` can still be found under launchd's or systemd's minimal environment) along with any of `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, and `NODE_EXTRA_CA_CERTS` you have set, and bakes them into the service definition. If you install or move a Node/provider CLI afterward, run `kessel stop` followed by `kessel start` again so the durable service picks up the new `PATH`.

### Alternative: Start Kessel before the application exists

In the first terminal, start a foreground owner with no application command:

```text
kessel run --provider codex
```

Leave that terminal open while creating or editing your application. When the application is ready, launch it from a second terminal:

```text
kessel run --provider codex -- python app.py
```

The second command detects the foreground Kessel session and attaches to it. Pressing Ctrl+C in the first terminal, or closing that terminal, stops Kessel and ends applications attached through `kessel run`.

The `--provider` value selects the OpenAI-compatible route supplied to the application. A running Kessel server can still serve both providers when both CLIs are installed.

## How client configuration works

`kessel run` supplies these variables only to the application it launches:

| Variable | Value |
| --- | --- |
| `OPENAI_BASE_URL` | `http://127.0.0.1:4880/v1/codex` or `/v1/claude` |
| `OPENAI_API_KEY` | The local Kessel key |
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:4880` |
| `ANTHROPIC_API_KEY` | The same local Kessel key |

The OpenAI route is selected by `--provider`. The Anthropic Messages route always uses Claude Code because Codex does not implement that API.

An application using the Anthropic SDK reads its standard variables in the same way:

```python
import os

from anthropic import Anthropic

client = Anthropic(
    base_url=os.environ["ANTHROPIC_BASE_URL"],
    api_key=os.environ["ANTHROPIC_API_KEY"],
)
```

`ANTHROPIC_API_KEY` also contains the local Kessel key, not an Anthropic-issued credential.

Kessel itself is stateless. Your application must send the prior messages with every request if it needs a conversation to appear stateful.

### Configure a tool that Kessel cannot launch

For an editor or another independently launched tool, first run durable Kessel:

```text
kessel start
```

Then print the exact local settings for that client:

```text
kessel connect cursor
kessel connect openai-python
kessel connect anthropic-python
```

Supported connection targets are `cursor`, `continue`, `aider`, `curl`, `openai-python`, `openai-node`, `anthropic-python`, and `anthropic-node`.

Both commands accept `--provider {codex,claude}` to choose which provider the OpenAI-compatible route points at. Without `--provider`, Kessel uses the sole configured provider when only one of Codex or Claude Code is set up, and `claude` otherwise. The Anthropic routes always use Claude Code regardless of `--provider`; when `--provider codex` is combined with an `anthropic-python` or `anthropic-node` target, `kessel connect` prints a note that Anthropic SDK routes always use Claude Code.

`kessel connect` prints the real local key, so treat its output as a credential. `kessel env` is also available for advanced shell workflows, but it is unnecessary when an application is launched through `kessel run`.

## Commands

| Command | Action |
| --- | --- |
| `kessel --version` | Show the installed Kessel version |
| `kessel setup` | Configure Kessel and test available providers without leaving it running |
| `kessel setup --port PORT` | Configure Kessel and save `PORT` (1-65535) as the port it listens on |
| `kessel accounts` | Show account information for installed provider CLIs |
| `kessel run --provider PROVIDER -- COMMAND` | Run an application with a temporary, foreground, or existing durable Kessel |
| `kessel run --provider PROVIDER` | Own a foreground Kessel session in the current terminal |
| `kessel start` | Install and start durable Kessel for the current user |
| `kessel stop` | Stop the current Kessel process |
| `kessel status` | Show whether Kessel is running, including a degraded reason |
| `kessel doctor` | Check provider installation, compatibility, and login state |
| `kessel connect TARGET [--provider PROVIDER]` | Print a client's local URL and Kessel key |
| `kessel env [--provider PROVIDER] [--shell SHELL]` | Print client environment variables (PowerShell syntax by default on Windows, POSIX elsewhere) |
| `kessel key --copy` | Copy the Kessel key without printing it |
| `kessel key --rotate` | Replace the saved Kessel key |
| `kessel logs [-n N]` | Print the last `N` lines (default 20) of the server log |
| `kessel uninstall-service` | Remove the durable service registration; keeps config, key, and logs |

`kessel accounts` works without a running Kessel server. It reports both
providers as authenticated, signed out, missing, or unavailable and includes
email, organization, plan, and authentication method when supplied by the CLI.
Account information is printed only to the invoking terminal.

The local web client is available at `http://127.0.0.1:4880` while Kessel is running. It shows the selected provider's signed-in account after the local Kessel key is entered. Interactive API documentation is at `http://127.0.0.1:4880/docs`.

## API routes

| Method | Path | Provider |
| --- | --- | --- |
| `POST` | `/v1/codex/chat/completions` | Codex through the OpenAI SDK format |
| `POST` | `/v1/claude/chat/completions` | Claude Code through the OpenAI SDK format |
| `POST` | `/v1/messages` | Claude Code through the Anthropic SDK format |
| `GET` | `/v1/providers/accounts` | Signed-in provider account information |
| `GET` | `/v1/{provider}/models` | Provider model discovery |
| `GET` | `/health` | Local service health |

`/health` returns HTTP 200 whenever Kessel is alive, with a JSON body of the
form `{"service": "kessel", "status": "ok" | "degraded", "providers": {...}}`.
`status` is `degraded`, with an explanatory `message`, when no provider is
currently available. `/health` does not require the Kessel API key and does
not expose versions or account information.

OpenAI-compatible clients send `Authorization: Bearer <kessel key>`. Anthropic clients send `X-API-Key: <kessel key>`. Kessel accepts either header on authenticated API routes.

`GET /v1/providers/accounts` returns normalized account information for both
provider CLIs. Each item includes an authentication status and, when supplied
by the provider, its authentication method, account type, email, organization,
and subscription. Fields that do not apply to the active login method are
`null`. The account route requires the Kessel API key; `/health` does not expose
account information.

```json
{
  "object": "list",
  "data": [
    {
      "provider": "codex",
      "status": "authenticated",
      "auth_method": "chatgpt",
      "account_type": "chatgpt",
      "email": "user@example.com",
      "organization": null,
      "subscription": "pro"
    }
  ]
}
```

Provider status is `authenticated`, `not_authenticated`, `not_installed`, or
`unavailable`. Kessel reads this information on demand and does not retain it.

### Important behavior

- Each request is independent. The default backend starts a fresh provider process for every request.
- OpenAI and Anthropic streaming formats are supported. Streamed Codex requests run on a one-shot Codex app-server, so text arrives as it is generated and generation stops as soon as `max_tokens`, a stop sequence, or a client disconnect ends the response.
- `max_tokens`, stop sequences, JSON objects, and JSON Schema output are enforced at Kessel's response boundary. Codex also enforces a schema natively when it meets OpenAI's strict rules (every object sets `additionalProperties: false` and lists all of its properties as required); any other schema is described to the model and validated by Kessel. Enforcing `max_tokens` or a stop sequence requires token-counting data that Kessel downloads once the first time it is needed; `kessel setup` pre-warms it so the first request does not pay that cost.
- Up to 16 OpenAI-style function tools are supported per request, with `tool_choice` of `auto`, `required`, `none`, or a named tool object (`{"type": "function", "function": {"name": "..."}}`). At most one tool call is returned per response, and `parallel_tool_calls` is not supported.
- The Anthropic Messages route accepts any `claude-*` model ID and rejects requests whose content includes image or document blocks.
- Codex supports an optional warm backend, but every warm request still receives a new ephemeral thread; the warm Codex process uses the account already signed in to the Codex CLI in place, without copying credentials, and it shuts itself down after 10 minutes of idle time. Warm and streamed Codex requests for the `default` model use the `model` set in your Codex `config.toml`, when one is set.
- Provider processes inherit `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, and `NODE_EXTRA_CA_CERTS` from Kessel's environment when those are set, so provider CLIs behind a corporate proxy or custom CA bundle keep working.
- Kessel must run as one Uvicorn worker because provider limits and warm Codex state are process-local.

Use `/docs` for complete request schemas and validation rules.

## Configuration and security

Configuration is stored in `%LOCALAPPDATA%\Kessel\config.json` on Windows and `$XDG_CONFIG_HOME/kessel/config.json`, normally `~/.config/kessel/config.json`, on macOS and Linux.

Kessel binds to `127.0.0.1`, authenticates every `/v1` route with its local key, and does not store prompts, responses, or provider account information. Provider credentials remain in the provider CLIs' existing authentication stores. Provider processes receive an allowlisted environment and run with tools, user rules, skills, MCP servers, and conversation persistence disabled by default. Codex always loads a global `AGENTS.md` from its home directory; Kessel instructs the model to disregard it so personal instructions do not change API responses.

When Kessel runs as a server (`kessel serve`, `kessel start`, or a temporary server owned by `kessel run`), it writes a rotating log file next to its configuration state (in the `logs` subdirectory), capped at 1 MB across 3 files. The log only ever records request method, path, and status; it never records prompts, responses, keys, or account information. Use `kessel logs` to print its tail.

Kessel protects a localhost service from accidental or unauthorized requests. It is not a security boundary against another process running as the same operating-system user because that process can ordinarily read the same local files.

Controlled overrides are documented in the application settings and include `KESSEL_API_KEY`, `KESSEL_CORS_ORIGINS`, provider concurrency limits, request timeouts, and provider executable paths. Most users do not need them.

See the [security policy](https://github.com/waderylan/kessel/blob/main/SECURITY.md)
for supported versions and private vulnerability reporting.

## Benchmarks

The live-provider benchmark loads the saved Kessel API key by default:

```powershell
python benchmarks/run.py --runs 5
```

Use the deterministic suite when measuring gateway, health-check, or process
launch changes without model and network variance:

```powershell
python benchmarks/local_overhead.py --runs 100
```

<!-- benchmark-table:start -->
| Provider | Backend | Effort | Runs | TTFT p50 | TTFT p95 | Total p50 | Total p95 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| codex | fresh | low | 3 | 2735 ms | 2884 ms | 3492 ms | 3499 ms |
| codex | fresh | medium | 3 | 3013 ms | 4604 ms | 4062 ms | 5214 ms |
| codex | fresh | high | 3 | 3137 ms | 4183 ms | 3716 ms | 4855 ms |
| codex | fresh | xhigh | 3 | 2721 ms | 4309 ms | 3318 ms | 5025 ms |
| codex | warm | low | 3 | 2486 ms | 2651 ms | 2814 ms | 2838 ms |
| codex | warm | medium | 3 | 2208 ms | 3442 ms | 2359 ms | 3635 ms |
| codex | warm | high | 3 | 2362 ms | 2623 ms | 2564 ms | 2801 ms |
| codex | warm | xhigh | 3 | 2530 ms | 3477 ms | 2719 ms | 3599 ms |
| claude | fresh | low | 3 | 1366 ms | 1503 ms | 1860 ms | 1987 ms |
| claude | fresh | medium | 3 | 1436 ms | 1703 ms | 1996 ms | 2195 ms |
| claude | fresh | high | 3 | 1409 ms | 1431 ms | 1921 ms | 1983 ms |
| claude | fresh | xhigh | 3 | 1362 ms | 1449 ms | 1961 ms | 2011 ms |
<!-- benchmark-table:end -->

### Live testing notes

These observations come from live CLI testing on Windows 11 with Codex
`0.156.0` and Claude Code `2.1.280` in September 2026.

| Measurement | Codex | Claude Code |
| --- | --- | --- |
| Prompt tokens for a one-word reply | About 6,200 | About 700 |
| Fresh request, full reply | About 4–6 s | About 1.5–3 s |
| Streamed fresh request, first text | About 10 s | About 1.5 s |
| Warm backend, first text | About 4.5 s | Not available |

- Codex adds its own base context of about 6,200 prompt tokens to every
  request, even a one-word reply. Disabling Codex's optional `include_*`
  instructions saves only about 200 of those tokens, so Kessel leaves them
  unchanged.
- The warm Codex backend removes most of Codex's per-request startup time.

### Known issues

- Replies that Kessel ends early because of `max_tokens` or a stop sequence
  report `prompt_tokens: 0` (`input_tokens: 0` on the Anthropic route).
  Tracked in [issue #1](https://github.com/waderylan/kessel/issues/1).

## Development

```powershell
git clone https://github.com/waderylan/kessel.git
cd kessel
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
```

Use `kessel setup` for an end-to-end provider test. `kessel serve` is an internal foreground server command intended only for debugging; normal local work should use `kessel run` or `kessel start`.

Releases publish to PyPI from CI: publishing a GitHub release triggers a workflow that builds the wheel and source distribution from the release tag and uploads them with trusted publishing. Local `python -m build`/`twine` runs are for verification, not for publishing.

## Provider terms

Kessel is an independent project and is not affiliated with, endorsed by, or
sponsored by OpenAI or Anthropic. Review the current
[provider terms assessment](https://github.com/waderylan/kessel/blob/main/PROVIDER_TERMS.md)
before distributing Kessel or changing its local, single-user operating
boundaries.

## License

Kessel is source-available under the
[PolyForm Noncommercial License 1.0.0](https://github.com/waderylan/kessel/blob/main/LICENSE).
Noncommercial use, modification, and distribution are permitted under that
license. Commercial use is reserved exclusively to Rylan Wade. This includes
selling Kessel,
offering paid or advertising-supported access to it, bundling it with a paid
product or service, using it to provide paid services, or using it for the
commercial benefit of a for-profit organization.

See the
[contribution policy](https://github.com/waderylan/kessel/blob/main/CONTRIBUTING.md)
before proposing a contribution.
