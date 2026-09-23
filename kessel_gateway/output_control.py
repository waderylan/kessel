"""Client-side output limits for provider text streams."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from kessel_gateway.models import ProviderResult, ProviderStreamEvent, TokenUsage


@lru_cache(maxsize=1)
def _encoding() -> Any:
    """Load the exact tokenizer only when an output limit needs it."""

    import tiktoken

    return tiktoken.get_encoding("o200k_base")


def estimate_tokens(text: str) -> int:
    """Estimate provider-independent output tokens with o200k_base."""

    return len(_encoding().encode(text))


def _decode_token_prefix(text: str, token_limit: int) -> str:
    encoding = _encoding()
    token_ids = encoding.encode(text)
    prefix_bytes = b"".join(
        encoding.decode_single_token_bytes(token_id)
        for token_id in token_ids[:token_limit]
    )
    try:
        prefix = prefix_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        prefix = prefix_bytes[: exc.start].decode("utf-8")
    if text.startswith(prefix):
        return prefix

    # This should only be reachable if a future tokenizer normalizes input.
    while prefix and not text.startswith(prefix):
        prefix = prefix[:-1]
    return prefix


@dataclass(frozen=True)
class ControlledText:
    emitted: str = ""
    finish_reason: str | None = None
    stop_sequence: str | None = None


class TextOutputController:
    """Incrementally enforce a token ceiling and literal stop sequences."""

    def __init__(
        self,
        max_tokens: int | None,
        stop_sequences: Sequence[str],
    ) -> None:
        self.max_tokens = max_tokens
        self.stop_sequences = tuple(stop_sequences)
        self._holdback = max((len(item) for item in self.stop_sequences), default=0) - 1
        self._pending = ""
        self.delivered = ""

    def push(self, text: str, *, final: bool = False) -> ControlledText:
        self._pending += text
        match = self._earliest_match()
        if match is not None:
            index, sequence = match
            candidate = self._pending[:index]
            self._pending = ""
            emitted, hit_limit = self._emit_with_limit(candidate)
            if hit_limit:
                return ControlledText(emitted=emitted, finish_reason="length")
            return ControlledText(
                emitted=emitted,
                finish_reason="stop",
                stop_sequence=sequence,
            )

        if final:
            candidate = self._pending
            self._pending = ""
        elif self._holdback:
            safe_length = max(0, len(self._pending) - self._holdback)
            candidate = self._pending[:safe_length]
            self._pending = self._pending[safe_length:]
        else:
            candidate = self._pending
            self._pending = ""

        emitted, hit_limit = self._emit_with_limit(candidate)
        return ControlledText(
            emitted=emitted,
            finish_reason="length" if hit_limit else None,
        )

    def _earliest_match(self) -> tuple[int, str] | None:
        matches = (
            (index, order, sequence)
            for order, sequence in enumerate(self.stop_sequences)
            if (index := self._pending.find(sequence)) >= 0
        )
        match = min(matches, default=None)
        if match is None:
            return None
        return match[0], match[2]

    def _emit_with_limit(self, text: str) -> tuple[str, bool]:
        if self.max_tokens is None:
            self.delivered += text
            return text, False

        combined = self.delivered + text
        token_count = estimate_tokens(combined)
        if token_count < self.max_tokens:
            self.delivered = combined
            return text, False
        if token_count == self.max_tokens:
            self.delivered = combined
            return text, True

        limited = _decode_token_prefix(combined, self.max_tokens)
        if not limited.startswith(self.delivered):
            return "", True
        emitted = limited[len(self.delivered) :]
        self.delivered = limited
        return emitted, True


def _terminated_usage(usage: TokenUsage | None, text: str) -> TokenUsage:
    prompt_tokens = usage.prompt_tokens if usage is not None else 0
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=estimate_tokens(text),
        total_tokens=prompt_tokens + estimate_tokens(text),
        prompt_tokens_details=(
            usage.prompt_tokens_details if usage is not None else None
        ),
    )


async def control_output_stream(
    source: AsyncIterator[ProviderStreamEvent],
    *,
    requested_model: str,
    max_tokens: int | None,
    stop_sequences: Sequence[str],
) -> AsyncIterator[ProviderStreamEvent]:
    """Yield controlled text and close the source immediately on termination."""

    controller = TextOutputController(max_tokens, stop_sequences)
    saw_delta = False
    source_closed = False

    async def close_source() -> None:
        nonlocal source_closed
        if source_closed:
            return
        source_closed = True
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()

    def terminated_result(
        controlled: ControlledText,
        base: ProviderResult | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            text=controller.delivered,
            model=base.model if base is not None else requested_model,
            usage=_terminated_usage(
                base.usage if base is not None else None,
                controller.delivered,
            ),
            finish_reason=controlled.finish_reason,
            stop_sequence=controlled.stop_sequence,
        )

    try:
        async for event in source:
            if event.delta:
                saw_delta = True
                controlled = controller.push(event.delta)
                if controlled.finish_reason is not None:
                    await close_source()
                    if controlled.emitted:
                        yield ProviderStreamEvent(delta=controlled.emitted)
                    yield ProviderStreamEvent(
                        result=terminated_result(controlled, event.result)
                    )
                    return
                if controlled.emitted:
                    yield ProviderStreamEvent(delta=controlled.emitted)

            if event.result is None:
                continue

            result = event.result
            if result.tool_calls:
                if max_tokens is not None:
                    tool_text = "".join(
                        tool.function.name + tool.function.arguments
                        for tool in result.tool_calls
                    )
                    tool_tokens = estimate_tokens(tool_text)
                    if tool_tokens >= max_tokens:
                        await close_source()
                        yield ProviderStreamEvent(
                            result=result.model_copy(
                                update={
                                    "text": "" if tool_tokens > max_tokens else None,
                                    "tool_calls": (
                                        None
                                        if tool_tokens > max_tokens
                                        else result.tool_calls
                                    ),
                                    "usage": _terminated_usage(
                                        result.usage,
                                        "" if tool_tokens > max_tokens else tool_text,
                                    ),
                                    "finish_reason": "length",
                                }
                            )
                        )
                        return
                yield event
                return

            if not saw_delta and result.text:
                controlled = controller.push(result.text, final=True)
            else:
                controlled = controller.push("", final=True)
            if controlled.finish_reason is not None:
                await close_source()
                if controlled.emitted:
                    yield ProviderStreamEvent(delta=controlled.emitted)
                yield ProviderStreamEvent(result=terminated_result(controlled, result))
                return
            if controlled.emitted:
                yield ProviderStreamEvent(delta=controlled.emitted)

            yield ProviderStreamEvent(
                result=result.model_copy(update={"text": controller.delivered})
            )
            return
    finally:
        await close_source()
