"""Per-fingerprint in-flight lock registry (threads only, no event loop).

Pins the registry contract from library/relay-dedup-design.md
("Concurrency"): identical fingerprints serialize, different
fingerprints never block each other, the lock is released on every
failure path (a failed first attempt never poisons the fingerprint),
and entries disappear once no waiters remain so the registry cannot
accumulate. Everything here is thread-based on purpose — the relay is
a single uvicorn worker, and loop-bound asyncio primitives are
forbidden by the binding harness rule.
"""

from __future__ import annotations

import threading

import pytest

from server.in_flight import FingerprintLocks

FP = "a" * 64  # the registry keys are opaque; the store validates digests
FP_OTHER = "b" * 64


def wait_until(predicate, timeout=10.0, message="condition never became true"):
    """Bounded polling from a test thread (fails loudly, never hangs)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError(message)


def test_hold_serializes_same_fingerprint_and_counts_waiters():
    locks = FingerprintLocks()
    order = []
    holder_inside = threading.Event()

    def waiter():
        with locks.hold(FP):
            order.append("waiter")

    with locks.hold(FP):
        assert locks.waiting(FP) == 0  # held, but nobody parked yet
        holder_inside.set()
        thread = threading.Thread(target=waiter)
        thread.start()
        # the waiter is visibly parked on the lock and cannot enter
        wait_until(lambda: locks.waiting(FP) == 1, message="waiter never parked")
        assert locks.size() == 1
        order.append("holder")

    thread.join(10)
    assert not thread.is_alive()
    assert order == ["holder", "waiter"]  # strict serialization
    assert locks.size() == 0  # entry removed once no waiters remain
    assert locks.waiting(FP) == 0


def test_failed_hold_releases_the_lock_and_registry_empties():
    locks = FingerprintLocks()
    with pytest.raises(RuntimeError):
        with locks.hold(FP):
            raise RuntimeError("upstream failed")
    # released on the failure path: a new hold acquires instantly
    with locks.hold(FP):
        pass
    assert locks.size() == 0


def test_failed_holder_releases_to_parked_waiter():
    """Unit twin of VAL-DEDUP-020: a failure frees the waiter, never blocks it."""
    locks = FingerprintLocks()
    entered = threading.Event()
    failures = []

    def failing_holder():
        try:
            with locks.hold(FP):
                entered.set()
                wait_until(lambda: locks.waiting(FP) == 1, message="no waiter parked")
                raise RuntimeError("upstream failed")
        except RuntimeError as exc:
            failures.append(str(exc))

    thread = threading.Thread(target=failing_holder)
    thread.start()
    entered.wait(10)
    with locks.hold(FP):  # acquires only after the failure path released
        pass

    thread.join(10)
    assert not thread.is_alive()
    assert failures == ["upstream failed"]
    assert locks.size() == 0


def test_different_fingerprints_do_not_serialize():
    locks = FingerprintLocks()
    holder_inside = threading.Event()
    release = threading.Event()

    def holder():
        with locks.hold(FP):
            holder_inside.set()
            assert release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    holder_inside.wait(10)
    with locks.hold(FP_OTHER):  # proceeds while FP is still held
        pass
    release.set()
    thread.join(10)
    assert not thread.is_alive()
    assert locks.size() == 0


def test_many_waiters_hand_through_and_registry_empties():
    locks = FingerprintLocks()
    entered = []

    def waiter(index):
        with locks.hold(FP):
            entered.append(index)

    with locks.hold(FP):
        threads = [threading.Thread(target=waiter, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        wait_until(lambda: locks.waiting(FP) == 3, message="not all waiters parked")

    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert sorted(entered) == [0, 1, 2]  # everybody got through
    assert locks.size() == 0


def test_sequential_holds_reuse_no_stale_entry():
    locks = FingerprintLocks()
    with locks.hold(FP):
        pass
    assert locks.size() == 0
    with locks.hold(FP):  # a fresh entry, no leak from the previous hold
        pass
    assert locks.size() == 0
    assert locks.waiting(FP_OTHER) == 0  # unknown fingerprints report zero
