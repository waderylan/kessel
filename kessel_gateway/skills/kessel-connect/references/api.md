# API routing and compatibility

Use the installed release's `/docs` schemas for details when needed. The
default root is `http://127.0.0.1:4880`; use the configured port, not a guessed
default. `kessel setup` and `kessel status` report the configured URL.

| Client API | Base URL | Method |
| --- | --- | --- |
| OpenAI Chat Completions through Codex | `<root>/v1/codex` | `chat.completions.create` |
| OpenAI Chat Completions through Claude Code | `<root>/v1/claude` | `chat.completions.create` |
| Anthropic Messages through Claude Code | `<root>` | `messages.create` |

The SDK appends the remaining path. Do not supply `/chat/completions` as the
SDK base URL or add `/v1` to the Anthropic base URL. Selecting
`kessel run --provider codex` changes only the OpenAI route; Anthropic requests
still require Claude Code.

`model="default"` is a useful first test. Discover model IDs through the
authenticated `GET /v1/{provider}/models` route rather than guessing current
model names. A client model selector must use an available ID.

## Supported behavior and common migration gaps

- Text Chat Completions and text Anthropic Messages; both support streaming.
- JSON objects and JSON Schema response formats are validated at the gateway.
  Schema references (`$ref`) are unsupported. Codex's native strict schema
  path additionally requires `additionalProperties: false` and every property
  listed in `required` at every object level. Do not silently remove constraints
  to get a successful response.
- Up to 16 function tools. The application executes requested tools and sends
  each result in the next request. Kessel's provider processes do not execute
  the application's tools or gain its filesystem access.
- Parallel calls require Chat Completions `parallel_tool_calls: true`, or
  Anthropic `tool_choice.disable_parallel_tool_use: false`.
- Strict function tool schemas (`strict: true`) are rejected. Report the
  incompatibility; do not quietly weaken the contract.
- Requests are stateless. Send full conversation history and tool results.
- Image/document input, embeddings, audio, file uploads, and the OpenAI
  Responses API are not implemented by these routes. Existing
  `client.responses.create(...)` calls need an explicit application adaptation;
  changing the base URL alone will not work.

Preserve application-required features during migration. If a requirement
cannot be met, name the gap and keep the existing working path available.
Kessel is a local single-user development gateway. Deployed or multi-user
applications continue to use provider-supported API authentication; do not
configure remote access to a personal subscription.

OpenAI-compatible requests authenticate with `Authorization: Bearer <local key>`;
Anthropic clients use `X-API-Key: <local key>`. Both use Kessel's own key.
`/health` needs no key, but an HTTP 200 with `status: degraded` does not prove a
provider request will succeed.
