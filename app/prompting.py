"""Serialize API requests into one stateless CLI prompt."""

from __future__ import annotations

import json

from app.models import ChatCompletionRequest, ChatMessage


ROLE_LABELS = {
    "developer": "DEVELOPER",
    "system": "SYSTEM",
    "user": "USER",
    "assistant": "ASSISTANT",
    "tool": "TOOL",
}


def build_prompt(request: ChatCompletionRequest) -> str:
    """Keep role boundaries visible when sending a conversation to a CLI."""

    preamble = (
        "Answer the following chat conversation. Treat DEVELOPER and SYSTEM "
        "messages as instructions. Return only the assistant response. Do not "
        "inspect the local machine, run commands, or use tools."
    )
    sections = [preamble]
    for message in request.messages:
        text = message.text().strip()
        if text:
            label = ROLE_LABELS[message.role]
            if message.tool_call_id:
                label = f"{label} call_id={message.tool_call_id}"
            sections.append(f"[{label}]\n{text}")

    active_tools = request.tools if request.tool_choice != "none" else []
    if active_tools:
        tool_payload = [tool.model_dump() for tool in active_tools]
        sections.append(
            "[AVAILABLE_FUNCTIONS]\n"
            + json.dumps(tool_payload, separators=(",", ":"))
        )
        choice_instruction = (
            "Call the function when it is useful."
            if request.tool_choice == "auto"
            else "You must call the supplied function."
        )
        sections.append(
            "[TOOL_INSTRUCTIONS]\n"
            f"{choice_instruction} Produce at most one function call."
        )
    sections.append("[ASSISTANT]")
    return "\n\n".join(sections)
