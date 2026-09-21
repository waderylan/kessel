from app.models import ChatCompletionRequest, ChatMessage
from app.prompting import build_prompt


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

    assert prompt.index("[SYSTEM]") < prompt.index("[USER]")
    assert prompt.count("[USER]") == 2
    assert prompt.endswith("[ASSISTANT]")
    assert "Do not inspect the local machine" in prompt


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
