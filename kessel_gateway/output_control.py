"""Client-side output limits for provider text streams."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from kessel_gateway.models import ProviderResult, ProviderStreamEvent, TokenUsage
from kessel_gateway.runner import ProcessError


class TokenizerUnavailableError(ProcessError):
    """The tiktoken encoding could not be loaded (e.g. no network on first use)."""

    status_code = 503
    error_code = "tokenizer_unavailable"
    public_message = (
        "Token counting data is unavailable. Connect to the internet once and "
        "run kessel setup."
    )


@lru_cache(maxsize=1)
def _encoding() -> Any:
    """Load the exact tokenizer only when an output limit needs it.

    ``lru_cache`` does not cache raised exceptions, so a failed load (for
    example, no network the first time tiktoken needs to download
    ``o200k_base``) is retried on the next call instead of failing forever.
    """

    import tiktoken

    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception as exc:
        raise TokenizerUnavailableError(
            f"failed to load the o200k_base tokenizer: {exc}"
        ) from exc


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


_TAIL_WINDOW_TOKENS = 16
_FREEZE_THRESHOLD_TOKENS = 48


class _RunningTokenCount:
    """Track an exact token count for a growing (append-only) string cheaply.

    Re-tokenizing the whole accumulated string on every chunk is quadratic in
    the number of chunks. Instead, once a prefix has enough tokens, its count
    is frozen and never re-tokenized; only the trailing window (the last
    ``_TAIL_WINDOW_TOKENS`` tokens' text, plus whatever is new) is re-encoded
    on each call, which keeps each call's work bounded.
    """

    def __init__(self) -> None:
        self._frozen_len = 0
        self._frozen_tokens = 0

    def count(self, text: str) -> int:
        """Return the exact token count of ``text``, a growing accumulator."""

        if len(text) < self._frozen_len:
            # Not append-only after all; drop the cache and recompute clean.
            self._frozen_len = 0
            self._frozen_tokens = 0
        pending = text[self._frozen_len :]
        pending_tokens = _encoding().encode(pending)
        if len(pending_tokens) > _FREEZE_THRESHOLD_TOKENS:
            pending_tokens = self._freeze(pending, pending_tokens)
        return self._frozen_tokens + len(pending_tokens)

    def _freeze(self, pending: str, pending_tokens: list[int]) -> list[int]:
        freeze_count = len(pending_tokens) - _TAIL_WINDOW_TOKENS
        frozen_text = _decode_token_prefix(pending, freeze_count)
        if not frozen_text:
            return pending_tokens
        self._frozen_len += len(frozen_text)
        self._frozen_tokens += len(_encoding().encode(frozen_text))
        remaining = pending[len(frozen_text) :]
        return _encoding().encode(remaining)


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
        self._delivered_byte_len = 0
        self._token_count = _RunningTokenCount()

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

        new_byte_len = len(text.encode("utf-8"))
        if self._delivered_byte_len + new_byte_len < self.max_tokens:
            # Token count never exceeds UTF-8 byte count for this byte-level
            # BPE, so we are provably still under the limit without having to
            # tokenize anything.
            self.delivered += text
            self._delivered_byte_len += new_byte_len
            return text, False

        combined = self.delivered + text
        token_count = self._token_count.count(combined)
        if token_count < self.max_tokens:
            self.delivered = combined
            self._delivered_byte_len += new_byte_len
            return text, False
        if token_count == self.max_tokens:
            self.delivered = combined
            self._delivered_byte_len += new_byte_len
            return text, True

        limited = _decode_token_prefix(combined, self.max_tokens)
        if not limited.startswith(self.delivered):
            return "", True
        emitted = limited[len(self.delivered) :]
        self.delivered = limited
        self._delivered_byte_len = len(limited.encode("utf-8"))
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
    reported_model = requested_model
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
            model=base.model if base is not None else reported_model,
            usage=_terminated_usage(
                base.usage if base is not None else None,
                controller.delivered,
            ),
            finish_reason=controlled.finish_reason,
            stop_sequence=controlled.stop_sequence,
        )

    try:
        async for event in source:
            if event.model:
                reported_model = event.model
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
