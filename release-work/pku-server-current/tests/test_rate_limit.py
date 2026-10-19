"""Sliding-window rate limiter unit tests (VAL-SECU-015 building block).

The relay's per-route limits are an in-memory sliding window keyed to
request context (library/relay-dedup-design.md, "Security hardening").
These unit tests pin the window mechanics the routes rely on: exactly
``limit`` hits fit in a window, the window slides so expired hits stop
counting, and distinct keys never share a bucket. The route-level 429s
live in test_relay_rate_limits.py.
"""

from __future__ import annotations

from server import rate_limit

WINDOW_SECONDS = 60.0


def make_clock(monkeypatch, start=0.0):
    """A controllable clock: tests advance it to prove the window slides."""
    state = {"now": float(start)}

    def monotonic():
        return state["now"]

    monkeypatch.setattr(rate_limit.time, "monotonic", monotonic)
    return state


def test_allow_fits_exactly_limit_hits_then_refuses(monkeypatch):
    make_clock(monkeypatch, 1000.0)
    limiter = rate_limit.SlidingWindowRateLimiter(
        limit=3, window_seconds=WINDOW_SECONDS
    )
    for _ in range(3):
        assert limiter.allow("ip-7.7.7.7") is True
    assert limiter.allow("ip-7.7.7.7") is False  # crossing the window -> 429
    # A refused hit is not recorded, so the key stays over limit.
    assert limiter.allow("ip-7.7.7.7") is False


def test_window_slides_so_expired_hits_do_not_count(monkeypatch):
    state = make_clock(monkeypatch, 1000.0)
    limiter = rate_limit.SlidingWindowRateLimiter(
        limit=2, window_seconds=WINDOW_SECONDS
    )
    assert limiter.allow("ip-1") is True
    assert limiter.allow("ip-1") is True
    assert limiter.allow("ip-1") is False
    # After the window passes, both hits have expired and the key is free.
    state["now"] += WINDOW_SECONDS + 1.0
    assert limiter.allow("ip-1") is True
    assert limiter.allow("ip-1") is True
    assert limiter.allow("ip-1") is False
    # A hit exactly at the window boundary still counts inside the window.
    state["now"] = 1000.0 + WINDOW_SECONDS
    limiter2 = rate_limit.SlidingWindowRateLimiter(
        limit=1, window_seconds=WINDOW_SECONDS
    )
    assert limiter2.allow("ip-1") is True
    assert limiter2.allow("ip-1") is False
    state["now"] += 60.0
    assert limiter2.allow("ip-1") is True


def test_distinct_keys_never_share_a_bucket(monkeypatch):
    make_clock(monkeypatch, 2000.0)
    limiter = rate_limit.SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
    assert limiter.allow("ip-1") is True
    assert limiter.allow("ip-1") is False
    # An unrelated identity has its own allowance (e.g. a second caller IP).
    assert limiter.allow("ip-2") is True
    assert limiter.allow("ip-2") is False
    assert limiter.allow("ip-1") is False
