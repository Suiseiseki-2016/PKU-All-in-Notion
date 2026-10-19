"""In-memory per-route sliding-window rate limiting.

Pinned by library/relay-dedup-design.md ("Security hardening"): the
relay rate-limits per route with an in-memory sliding window keyed to
the request context (/v1/activate + /v1/redeem per IP; /v1/transcribe,
/v1/llm and /v1/notion/exchange per account). Sliding rather than
fixed-bucket so a caller straddling two window boundaries can never
double its allowance. Memory-only by design: the limits protect a
single relay process and reset with it, and no new dependency is
introduced.

The registry is thread-safe on purpose -- the relay serves its sync
routes on a threadpool and genuinely overlapping requests must
serialize (library/user-testing.md harness rule: thread-based, never
loop-bound asyncio).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    """Fixed-size sliding window keyed by identity (client IP or account id).

    ``allow(key)`` records a hit inside the window and returns True; a
    key that already holds the full ``limit`` of hits inside the
    window returns False (the caller answers 429). A refused hit is not
    recorded, so a caller cannot extend its own penalty window by
    hammering the endpoint.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0):
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """Record a hit for ``key`` inside the window; False when over limit."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = self._hits[key]
            # Drop hits that have left the window (sliding, not bucket).
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True
