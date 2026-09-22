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

Kessel also requires Python 3.10 or later and [pipx](https://pipx.pypa.io/).

### 2. Install Kessel

```text
pipx install kessel-local
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

Closing the terminal or pressing Ctrl+C also ends the managed application and temporary Kessel server. Kessel never changes your global or parent-terminal environment.

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

`kessel start` registers Kessel as a login-scoped service for the current operating-system user. It can start again at the next login after `kessel stop`; the stop command ends the current service process without deleting its registration, configuration, or API key.

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
| `OPENAI_BASE_URL` | `http://127.0.0.1:8000/v1/codex` or `/v1/claude` |
| `OPENAI_API_KEY` | The local Kessel key |
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:8000` |
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

`kessel connect` prints the real local key, so treat its output as a credential. `kessel env` is also available for advanced shell workflows, but it is unnecessary when an application is launched through `kessel run`.

## Commands

| Command | Action |
| --- | --- |
| `kessel setup` | Configure Kessel and test available providers without leaving it running |
| `kessel accounts` | Show account information for installed provider CLIs |
| `kessel run --provider PROVIDER -- COMMAND` | Run an application with a temporary, foreground, or existing durable Kessel |
| `kessel run --provider PROVIDER` | Own a foreground Kessel session in the current terminal |
| `kessel start` | Install and start durable Kessel for the current user |
| `kessel stop` | Stop the current Kessel process |
| `kessel status` | Show whether Kessel is running |
| `kessel doctor` | Check provider installation and login state |
| `kessel connect TARGET` | Print a client's local URL and Kessel key |
| `kessel key --copy` | Copy the Kessel key without printing it |
| `kessel key --rotate` | Replace the saved Kessel key |

`kessel accounts` works without a running Kessel server. It reports both
providers as authenticated, signed out, missing, or unavailable and includes
email, organization, plan, and authentication method when supplied by the CLI.
Account information is printed only to the invoking terminal.

The local web client is available at `http://127.0.0.1:8000` while Kessel is running. It shows the selected provider's signed-in account after the local Kessel key is entered. Interactive API documentation is at `http://127.0.0.1:8000/docs`.

## API routes

| Method | Path | Provider |
| --- | --- | --- |
| `POST` | `/v1/codex/chat/completions` | Codex through the OpenAI SDK format |
| `POST` | `/v1/claude/chat/completions` | Claude Code through the OpenAI SDK format |
| `POST` | `/v1/messages` | Claude Code through the Anthropic SDK format |
| `GET` | `/v1/providers/accounts` | Signed-in provider account information |
| `GET` | `/v1/{provider}/models` | Provider model discovery |
| `GET` | `/health` | Local service health |

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
- OpenAI and Anthropic streaming formats are supported.
- `max_tokens`, stop sequences, JSON objects, and JSON Schema output are enforced at Kessel's response boundary.
- One required function tool is supported per request. Parallel or optional tool selection is not supported.
- Codex supports an optional warm backend, but every warm request still receives a new ephemeral thread.
- Kessel must run as one Uvicorn worker because provider limits and warm Codex state are process-local.

Use `/docs` for complete request schemas and validation rules.

## Configuration and security

Configuration is stored in `%LOCALAPPDATA%\Kessel\config.json` on Windows and `$XDG_CONFIG_HOME/kessel/config.json`, normally `~/.config/kessel/config.json`, on macOS and Linux.

Kessel binds to `127.0.0.1`, authenticates every `/v1` route with its local key, and does not store prompts, responses, or provider account information. Provider credentials remain in the provider CLIs' existing authentication stores. Provider processes receive an allowlisted environment and run with tools, user rules, skills, MCP servers, and conversation persistence disabled by default.

Kessel protects a localhost service from accidental or unauthorized requests. It is not a security boundary against another process running as the same operating-system user because that process can ordinarily read the same local files.

Controlled overrides are documented in the application settings and include `KESSEL_API_KEY`, `KESSEL_CORS_ORIGINS`, provider concurrency limits, request timeouts, and provider executable paths. Most users do not need them.

See [AUDIT.md](AUDIT.md) for the full threat model and verified controls.

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
