"""Provider quota snapshots and HTTP header serialization."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitSnapshot:
    """A percentage-based subscription quota window."""

    remaining_percent: float
    resets_at: int | None = None
    limit_id: str | None = None
    retry_after_seconds: int | None = None
    # When set, the provider's own verdict on whether requests are blocked.
    # A spent window is not blocking while paid credits cover further usage.
    blocked: bool | None = None

    @property
    def exhausted(self) -> bool:
        if self.blocked is not None:
            return self.blocked
        return self.remaining_percent <= 0

    def headers(self) -> dict[str, str]:
        remaining = max(0, min(100, math.floor(self.remaining_percent)))
        headers = {
            "ratelimit-limit": "100",
            "ratelimit-remaining": str(remaining),
            "x-kessel-quota-remaining-percent": str(remaining),
        }
        if self.limit_id:
            headers["x-kessel-quota-limit-id"] = self.limit_id
        retry_after = self.retry_after_seconds
        if retry_after is None and self.resets_at is not None:
            retry_after = max(0, self.resets_at - int(time.time()))
        if self.resets_at is not None:
            headers["x-kessel-quota-reset-at"] = str(self.resets_at)
        if retry_after is not None:
            headers["ratelimit-reset"] = str(retry_after)
        if self.exhausted and retry_after is not None:
            headers["retry-after"] = str(max(1, retry_after))
        return headers
