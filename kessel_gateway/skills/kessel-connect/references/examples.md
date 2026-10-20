# Minimal application connections

Use the project's existing package manager and dependency conventions. Add
only the requested SDK if missing. Choose one example matching the project;
do not generate four starters. Preserve the application's existing provider
configuration mechanism when adapting existing code.

These examples read keys from the child environment. Do not replace those
reads with a literal key. `kessel run` sets all four SDK variables for its child
and leaves the parent shell unchanged.

## Python / OpenAI Chat Completions

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["OPENAI_BASE_URL"],
    api_key=os.environ["OPENAI_API_KEY"],
)
response = client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "Reply with OK."}],
)
print(response.choices[0].message.content)
```

Launch: `kessel run --provider codex -- python app.py`. Use the project's
actual interpreter or runner if it manages its own virtualenv.

## Node / OpenAI Chat Completions

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: process.env.OPENAI_BASE_URL,
  apiKey: process.env.OPENAI_API_KEY,
});
const response = await client.chat.completions.create({
  model: "default",
  messages: [{ role: "user", content: "Reply with OK." }],
});
console.log(response.choices[0].message.content);
```

For a new standalone file use `app.mjs`, or follow the project's module mode.
Launch: `kessel run --provider codex -- node app.mjs`.

## Python / Anthropic Messages

```python
import os
from anthropic import Anthropic

client = Anthropic(
    base_url=os.environ["ANTHROPIC_BASE_URL"],
    api_key=os.environ["ANTHROPIC_API_KEY"],
)
response = client.messages.create(
    model="default",
    max_tokens=32,
    messages=[{"role": "user", "content": "Reply with OK."}],
)
for block in response.content:
    if block.type == "text":
        print(block.text)
```

Launch: `kessel run --provider claude -- python app.py`.

## Node / Anthropic Messages

```javascript
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic({
  baseURL: process.env.ANTHROPIC_BASE_URL,
  apiKey: process.env.ANTHROPIC_API_KEY,
});
const response = await client.messages.create({
  model: "default",
  max_tokens: 32,
  messages: [{ role: "user", content: "Reply with OK." }],
});
for (const block of response.content) {
  if (block.type === "text") console.log(block.text);
}
```

Launch: `kessel run --provider claude -- node app.mjs`.

## Feature verification

For streaming, consume SDK events and verify incremental text and clean stream
completion. For structured output, validate the returned object against the
application's schema. For tools, exercise a harmless function, return its result
in the next request with its matching call ID, and verify the final response.
For parallel tools, answer every call individually. Do not equate receiving a
tool-call object with successfully completing a tool loop.
