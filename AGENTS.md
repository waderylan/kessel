# Kessel repository instructions

- Keep the API stateless: fresh processes are the default. Warm backends must create a new ephemeral thread per request and never resume prior state.
- Keep provider-specific CLI commands and response parsing inside `app/providers/`.
- Preserve the OpenAI Chat Completions request and response shape for public endpoints.
- Default to localhost-only operation and do not expose CLI credentials or authentication files.
- Use safe subprocess APIs with argument arrays. Never interpolate user input into a shell command.
- Keep the frontend dependency-free and accessible. Support loading, empty, success, and error states.
- Add or update tests when changing provider adapters, prompt serialization, or API contracts.
- Use concise commit subjects followed by a body containing 1-4 `-` bullets.
