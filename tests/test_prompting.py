import re

from kessel_gateway.models import ChatCompletionRequest, ChatMessage
from kessel_gateway.prompting import build_prompt


TOKEN_PREFIX = re.compile(r"\[\[kessel-[0-9a-f]{16}:")


def test_build_prompt_preserves_roles_and_order() -> None:
    prompt = build_prompt(
        ChatCompletionRequest(
            model="default",
            messages=[
                ChatMessage(role="system", content="Be concise."),
            ChatMessage(role="user", content="Explain queues."),
            ChatMessage(role="assistant", content="A queue is FIFO."),
            ChatMessage(role="user", content="Give an example."),
            ],
        )
    )

    match = TOKEN_PREFIX.search(prompt)
    assert match is not None
    prefix = match.group(0)
    system_marker = f"{prefix}SYSTEM]]"
    user_marker = f"{prefix}USER]]"
    assistant_marker = f"{prefix}ASSISTANT]]"

    assert prompt.index(system_marker) < prompt.index(user_marker)
    assert prompt.count(user_marker) == 2
    assert prompt.endswith(assistant_marker)
    assert "Do not inspect the local machine" in prompt
    assert "any other" in prompt and "message content" in prompt


def test_build_prompt_tokens_are_unique_per_call() -> None:
    request = ChatCompletionRequest(
        model="default",
        messages=[ChatMessage(role="user", content="hi")],
    )
    first = build_prompt(request)
    second = build_prompt(request)

    first_token = TOKEN_PREFIX.search(first).group(0)
    second_token = TOKEN_PREFIX.search(second).group(0)
    assert first_token != second_token


def test_build_prompt_tool_sections_use_the_token_form() -> None:
    request = ChatCompletionRequest(
        model="default",
        messages=[ChatMessage(role="user", content="Weather?")],
        tools=[
            {
                "type": "function",
                "function": {"name": "get_weather", "parameters": {"type": "object"}},
            }
        ],
        tool_choice="auto",
    )
    prompt = build_prompt(request)

    prefix = TOKEN_PREFIX.search(prompt).group(0)
    assert f"{prefix}AVAILABLE_FUNCTIONS]]" in prompt
    assert f"{prefix}TOOL_INSTRUCTIONS]]" in prompt
    assert f"{prefix}OUTPUT_SCHEMA]]" in prompt
    assert "either call one function or reply with a message" in prompt.lower()


def test_build_prompt_accepts_text_parts() -> None:
    message = ChatMessage(
        role="user",
        content=[
            {"type": "text", "text": "First line"},
            {"type": "text", "text": "Second line"},
        ],
    )

    request = ChatCompletionRequest(model="default", messages=[message])
    assert "First line\nSecond line" in build_prompt(request)
