"""Per-fingerprint in-flight serialization for concurrent identical misses.

Two genuinely overlapping requests for the same exact audio must cost
exactly one upstream transcription and one charge. This module is the
process-level half of that guarantee: a ``threading.Lock`` per
fingerprint serializes the miss path, and the double-check lookup
after the acquire turns the second caller into a hit of the first
caller's committed result.

Thread-based on purpose (library/relay-dedup-design.md, "Concurrency"):
the relay runs as a single uvicorn worker, so an in-process lock is
sufficient, and loop-bound asyncio primitives would break under the
threaded portal test harness. A multi-worker deployment would need a
SQLite lease (``BEGIN IMMEDIATE``) instead — deliberately not built
now; the store's fingerprint PK upsert remains the cross-process
backstop.

Failure policy (waiter fall-through): a failed first attempt never
poisons the fingerprint. The lock is released on every failure path,
and each waiter then runs its own upstream attempt, charging only on
its own success — failures are never inherited between requests.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class _Entry:
    """One in-flight fingerprint: its lock plus the callers parked on it."""

    __slots__ = ("lock", "waiters")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.waiters = 0


class FingerprintLocks:
    """Guarded registry of per-fingerprint in-flight locks.

    ``hold(fp)`` acquires the fingerprint's lock and always releases
    it; entries are removed once no waiters remain, so the registry
    cannot accumulate. The removal happens under the guard BEFORE the
    final release, so a late arrival gets a fresh entry and can never
    overtake the last holder's committed work.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def waiting(self, fingerprint: str) -> int:
        """Callers currently parked on this fingerprint's lock.

        Zero while the lock is merely held: ``waiters`` counts blocked
        acquire attempts only, and a holder's own count is retired on
        hand-over. Test/ops surface for proving genuine overlap.
        """
        with self._guard:
            entry = self._entries.get(fingerprint)
            return entry.waiters if entry is not None else 0

    def size(self) -> int:
        """Entries currently registered (zero when nothing is in flight)."""
        with self._guard:
            return len(self._entries)

    @contextmanager
    def hold(self, fingerprint: str) -> Iterator[None]:
        """Hold the fingerprint's in-flight lock; always released.

        The body runs serialized against every other ``hold`` of the
        same fingerprint; different fingerprints never interact. Every
        failure path (exception, return, re-raise) passes through the
        same release, and the registry entry is dropped when the last
        user is done.
        """
        with self._guard:
            entry = self._entries.get(fingerprint)
            if entry is None:
                entry = _Entry()
                self._entries[fingerprint] = entry
            entry.waiters += 1
        entry.lock.acquire()
        try:
            with self._guard:
                entry.waiters -= 1  # holder now, no longer a waiter
            yield
        finally:
            with self._guard:
                if entry.waiters == 0 and self._entries.get(fingerprint) is entry:
                    del self._entries[fingerprint]
            entry.lock.release()
