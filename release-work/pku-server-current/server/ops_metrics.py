"""Durable operator metrics without storing audio or transcript identities."""
from __future__ import annotations

import os
import sqlite3
import time
from decimal import Decimal, InvalidOperation

DEFAULT_ASR_RMB_PER_SECOND = Decimal("0.00022")
RATE_ENV = "ASR_COST_RMB_PER_SECOND"

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_cache_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    reused INTEGER NOT NULL CHECK (reused IN (0, 1)),
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds >= 0),
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS transcript_cache_events_recorded
    ON transcript_cache_events(recorded_at);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def record_cache_result(
    conn: sqlite3.Connection, account_id: int, *, reused: bool, duration_seconds: int
) -> None:
    if duration_seconds < 0:
        raise ValueError("duration_seconds must be non-negative")
    conn.execute(
        "INSERT INTO transcript_cache_events "
        "(account_id, reused, duration_seconds, recorded_at) VALUES (?, ?, ?, ?)",
        (account_id, int(reused), duration_seconds, time.time()),
    )
    conn.commit()


def asr_rate() -> Decimal:
    raw = os.environ.get(RATE_ENV, str(DEFAULT_ASR_RMB_PER_SECOND))
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{RATE_ENV} must be a non-negative decimal") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"{RATE_ENV} must be a non-negative decimal")
    return value


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def overview(conn: sqlite3.Connection, *, rate: Decimal | None = None) -> dict:
    """Aggregate known usage; provider invoices and revenue are separate data."""
    unit_rate = rate if rate is not None else asr_rate()
    if not unit_rate.is_finite() or unit_rate < 0:
        raise ValueError("ASR rate must be a non-negative decimal")

    accounts = conn.execute(
        "SELECT COUNT(*) total, "
        "COALESCE(SUM(status = 'active'), 0) active, "
        "COALESCE(SUM(email IS NOT NULL AND email_verified_at IS NULL), 0) unverified "
        "FROM accounts"
    ).fetchone()
    codes = conn.execute(
        "SELECT COUNT(*) total, "
        "COALESCE(SUM(redeemed_by IS NOT NULL), 0) redeemed FROM codes"
    ).fetchone()
    asr = conn.execute(
        "SELECT COUNT(*) calls, COALESCE(SUM(seconds), 0) seconds "
        "FROM usage WHERE kind = 'transcribe'"
    ).fetchone()
    llm = conn.execute(
        "SELECT COUNT(*) calls, COALESCE(SUM(input_tokens), 0) input_tokens, "
        "COALESCE(SUM(output_tokens), 0) output_tokens, "
        "COALESCE(SUM(millipoints), 0) millipoints FROM llm_usage"
    ).fetchone()
    cache = conn.execute(
        "SELECT COUNT(*) requests, COALESCE(SUM(reused), 0) hits, "
        "COALESCE(SUM(CASE WHEN reused = 1 THEN duration_seconds ELSE 0 END), 0) saved_seconds, "
        "MIN(recorded_at) tracking_since FROM transcript_cache_events"
    ).fetchone()
    cached_objects = conn.execute(
        "SELECT COUNT(*) FROM transcript_reuse WHERE expires_at > ?", (time.time(),)
    ).fetchone()[0]
    requests = int(cache["requests"])
    hits = int(cache["hits"])
    saved_seconds = int(cache["saved_seconds"])
    asr_seconds = int(asr["seconds"])
    return {
        "accounts": {
            "total": int(accounts["total"]),
            "active": int(accounts["active"]),
            "unverified": int(accounts["unverified"]),
        },
        "codes": {
            "total": int(codes["total"]),
            "redeemed": int(codes["redeemed"]),
            "available": int(codes["total"]) - int(codes["redeemed"]),
        },
        "asr": {
            "upstream_calls": int(asr["calls"]),
            "charged_seconds": asr_seconds,
            "unit_cost_rmb_per_second": str(unit_rate),
            "estimated_cost_rmb": _money(Decimal(asr_seconds) * unit_rate),
        },
        "llm": {
            "calls": int(llm["calls"]),
            "input_tokens": int(llm["input_tokens"]),
            "output_tokens": int(llm["output_tokens"]),
            "charged_millipoints": int(llm["millipoints"]),
            "estimated_cost_rmb": _money(
                Decimal(int(llm["millipoints"])) / Decimal("10000")
            ),
        },
        "cache": {
            "requests_since_tracking": requests,
            "hits": hits,
            "misses": requests - hits,
            "hit_rate": round(hits / requests, 4) if requests else None,
            "saved_seconds": saved_seconds,
            "estimated_saved_cost_rmb": _money(Decimal(saved_seconds) * unit_rate),
            "live_cached_objects": int(cached_objects),
            "tracking_since": cache["tracking_since"],
        },
        "estimated_total_cost_rmb": _money(
            Decimal(asr_seconds) * unit_rate
            + Decimal(int(llm["millipoints"])) / Decimal("10000")
        ),
        "cost_basis": "estimate_from_metered_usage; excludes free tier, discounts, OSS, and revenue",
    }
