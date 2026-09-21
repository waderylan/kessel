# Kessel

Kessel exposes locally installed Codex and Claude Code subscriptions through familiar HTTP APIs. An application can use the OpenAI or Anthropic SDK against `127.0.0.1`, while Kessel runs the matching provider CLI with the account that is already logged in on the same computer.

Kessel is for local development. It is not a hosted API, does not make a local subscription available to a remote deployment, and does not replace an OpenAI or Anthropic API account for production use.

## Setup

### What Kessel does

Many libraries know how to call the OpenAI Chat Completions API or the Anthropic Messages API, but they cannot call the Codex and Claude Code CLIs directly. Kessel is a local compatibility layer between those clients and CLIs:

```text
OpenAI client    -> Kessel /v1/codex  -> Codex CLI  -> ChatGPT subscription
OpenAI client    -> Kessel /v1/claude -> Claude CLI -> Claude subscription
Anthropic client -> Kessel /v1/messages -> Claude CLI -> Claude subscription
```

Each request is independent. Kessel does not retain conversations, and the default backend starts a fresh provider process for every request.

Kessel creates its own local API key during setup. This `kessel_...` key protects the service running on your computer. It is not an OpenAI or Anthropic API key. Client examples place it in `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` only because those are the standard variable names understood by the corresponding SDKs.

### 1. Install and log in to a provider

Install at least one provider CLI:

| Provider | Install | Log in |
| --- | --- | --- |
| Claude Code | `npm install -g @anthropic-ai/claude-code` | `claude login` |
| Codex | `npm install -g @openai/codex` | `codex login` |

Kessel requires Python 3.10 or later and [pipx](https://pipx.pypa.io/).

### 2. Install and start Kessel

```text
pipx install kessel-local
kessel setup
```

`kessel setup`:

- Checks which provider CLIs are installed and logged in.
- Creates a local `kessel_...` API key.
- Installs and starts a per-user background service.
- Sends a small test request through each available provider.
- Prints the local service URLs without printing the key.

The command is safe to run again. It preserves the existing key and repairs a stopped or missing service.

### 3. Connect a client

Ask Kessel for instructions for the client you use:

```text
kessel connect openai-python
```

Available targets:

| Target | Output is intended for |
| --- | --- |
| `openai-python` | Python using the OpenAI SDK |
| `openai-node` | JavaScript or TypeScript using the OpenAI SDK |
| `anthropic-python` | Python using the Anthropic SDK |
| `anthropic-node` | JavaScript or TypeScript using the Anthropic SDK |
| `curl` | A terminal |
| `cursor` | Cursor Settings > Models > API Keys |
| `continue` | Continue's `config.yaml` |
| `aider` | The terminal where Aider runs |

The command prints the real local URL and Kessel key, so treat its output as a credential.
OpenAI-based connection targets use the Claude route by default. Change
`/v1/claude` to `/v1/codex` when the client should use Codex instead.

## Choosing a provider

Kessel has two OpenAI-compatible routes:

| Base URL | Provider used |
| --- | --- |
| `http://127.0.0.1:8000/v1/codex` | Codex CLI |
| `http://127.0.0.1:8000/v1/claude` | Claude Code CLI |

The Anthropic-compatible route is:

```text
http://127.0.0.1:8000
```

It sends `/v1/messages` requests through Claude Code. Codex does not implement the Anthropic Messages route.

### Environment variables

`kessel env` prints the standard environment variables expected by OpenAI and Anthropic clients. The default OpenAI-compatible provider is Claude:

```sh
eval "$(kessel env)"
```

To use Codex for OpenAI-compatible requests:

```sh
eval "$(kessel env --provider codex)"
```

For PowerShell or Fish, print assignments in the matching syntax and apply the displayed commands in your shell:

```text
kessel env --provider codex --shell powershell
kessel env --provider codex --shell fish
```

The output sets:

- `OPENAI_BASE_URL` to the selected `/v1/codex` or `/v1/claude` route.
- `OPENAI_API_KEY` to the local Kessel key.
- `ANTHROPIC_BASE_URL` to the Kessel service root.
- `ANTHROPIC_API_KEY` to the same local Kessel key.

These variable names describe the client API format. They do not mean the credential was issued by OpenAI or Anthropic.

## Client examples

Run `kessel env` first, or set the shown variables for the current process.

### OpenAI SDK through Codex

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1/codex",
    # This variable contains the local kessel_... key.
    api_key=os.environ["OPENAI_API_KEY"],
)

response = client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "Explain this traceback."}],
    max_tokens=400,
)

print(response.choices[0].message.content)
```

Change the base URL to `/v1/claude` to use Claude Code through the same OpenAI SDK.

### Anthropic SDK through Claude Code

```python
import os

from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:8000",
    # This variable also contains the local kessel_... key.
    api_key=os.environ["ANTHROPIC_API_KEY"],
)

message = client.messages.create(
    model="default",
    max_tokens=400,
    messages=[{"role": "user", "content": "Explain this traceback."}],
)

print(message.content[0].text)
```

## Service commands

| Command | Action |
| --- | --- |
| `kessel status` | Show whether Kessel is running and print its URL |
| `kessel start` | Install the per-user service if needed, then start it |
| `kessel stop` | Stop the service without deleting configuration |
| `kessel doctor` | Recheck provider installation and login |
| `kessel key` | Print the local API key |
| `kessel key --copy` | Copy the key without printing it |
| `kessel key --rotate` | Replace the saved key immediately |

The local web client is at `http://127.0.0.1:8000`. Interactive OpenAPI documentation is at `http://127.0.0.1:8000/docs`.

Configuration is stored in `%LOCALAPPDATA%\Kessel\config.json` on Windows and `$XDG_CONFIG_HOME/kessel/config.json`, normally `~/.config/kessel/config.json`, on macOS and Linux.

## API reference

### Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/v1/codex/chat/completions` | OpenAI Chat Completions through Codex |
| `POST` | `/v1/claude/chat/completions` | OpenAI Chat Completions through Claude Code |
| `POST` | `/v1/messages` | Anthropic Messages through Claude Code |
| `GET` | `/v1/{provider}/models` | Provider model discovery |
| `GET` | `/health` | Provider availability without account details |

OpenAI-compatible clients send `Authorization: Bearer <kessel key>`. Anthropic clients send `X-API-Key: <kessel key>`. Kessel accepts either header on authenticated API routes.

All responses include `X-Request-ID`. Anthropic responses also include the compatible `request-id` header.

### Supported behavior

- OpenAI Chat Completions accepts `developer`, `system`, `user`, `assistant`, and `tool` messages.
- OpenAI and Anthropic streaming formats are supported.
- `max_tokens`, stop sequences, JSON objects, and JSON Schema output are enforced at Kessel's response boundary.
- One required function tool is supported per request. Parallel or optional tool selection is not supported.
- `backend="fresh"` is the default. Codex also supports `backend="warm"`; every warm request still receives a new ephemeral thread.
- Codex model discovery comes from its App Server. Claude model discovery contains only model IDs observed in successful requests during the current Kessel process.
- Each provider has an independent concurrency limit. Busy or quota-exhausted providers return `429` with `Retry-After` when a reset time is available.

Use the live OpenAPI page at `/docs` for complete request schemas and validation rules.

### Important limitations

- Kessel is stateless. Applications must send all conversation history needed for each request.
- Output-token limits are approximate because provider CLIs do not expose reliable generation-time token counts.
- Fresh Codex normally returns one completed content chunk rather than true token-by-token deltas.
- Stop sequences cannot be combined with structured output or tools.
- Strict tool schemas, parallel tool calls, and more than one active function are not supported.
- Kessel must run as one Uvicorn worker because provider limits, request tracking, quota state, and the warm Codex server are process-local.

## Configuration

Most users should use `kessel setup` and the service commands. These environment variables are for controlled overrides:

| Variable | Default | Purpose |
| --- | --- | --- |
| `KESSEL_API_KEY` | Saved config value | Override the local service credential |
| `KESSEL_CORS_ORIGINS` | Loopback origins | Set allowed browser origins |
| `KESSEL_REQUEST_TIMEOUT_SECONDS` | `300` | Set the provider request timeout |
| `KESSEL_CODEX_MAX_CONCURRENT_REQUESTS` | `2` | Set the Codex concurrency limit |
| `KESSEL_CLAUDE_MAX_CONCURRENT_REQUESTS` | `2` | Set the Claude concurrency limit |
| `KESSEL_PROVIDER_SLOT_WAIT_SECONDS` | `5` | Set the maximum wait for a provider slot |
| `KESSEL_CODEX_COMMAND` | Saved executable path or `codex` | Override the Codex executable |
| `KESSEL_CLAUDE_COMMAND` | Saved executable path or `claude` | Override the Claude executable |
| `KESSEL_ENFORCE_CLI_VERSIONS` | `true` | Enforce the tested provider CLI versions |

## Security

- Kessel binds to `127.0.0.1` and rejects non-loopback host headers.
- Every `/v1` route requires the local Kessel key.
- Prompts and responses are not stored or written to request logs.
- Provider credentials remain in their existing CLI authentication stores.
- Provider processes receive an allowlisted environment, not the complete Kessel environment.
- Fresh requests use temporary working directories and disable provider tools, user rules, skills, MCP servers, and conversation persistence.
- Client disconnects and server shutdown cancel the associated provider work.

Kessel protects a localhost service from accidental or unauthorized requests. It is not a security boundary against another process already running as the same operating-system user, because that process can ordinarily read the same local configuration and provider authentication files.

See [AUDIT.md](AUDIT.md) for the threat model, verified controls, residual risks, and reproduction commands.

## Development

Install a source checkout:

```powershell
git clone https://github.com/waderylan/kessel.git
cd kessel
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
```

Use `kessel setup` for an end-to-end local test. Use `kessel serve` only when debugging the foreground Uvicorn process.

Repository layout:

```text
app/cli.py                      Setup, environment, connection, and service commands
app/main.py                     HTTP routes and response streaming
app/models.py                   OpenAI and Anthropic request models
app/output_control.py           Token ceilings and stop-sequence matching
app/providers/                  Provider commands, health checks, and parsing
app/runner.py                   Bounded asynchronous subprocess execution
app/static/                     Dependency-free local web client
benchmarks/                     Latency harness and generated results
tests/                          API, provider, cancellation, and UI tests
```

Local latency measurements are available in [benchmarks/results.md](benchmarks/results.md). They are development measurements, not provider performance guarantees.
