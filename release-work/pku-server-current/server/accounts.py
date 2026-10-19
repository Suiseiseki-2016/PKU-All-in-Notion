"""Platform accounts for the M2 cloud service (docs/SERVICE_PLAN.md §1.7).

The classmate-facing product holds no keys of any kind: accounts live on
our server, the app logs in at activation, and the session token stored
locally is just an opaque credential. Transcription-minute and LLM quotas
ride the same account. SQLite keeps the first deployment a single file;
class-scale traffic makes that a non-issue, and everything sits behind
these functions if the storage ever needs swapping.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import re
import secrets
import sqlite3
import time
from pathlib import Path

from . import ops_metrics, transcript_store

SESSION_TTL_SECONDS = 30 * 24 * 3600  # sessions outlive quota refills
WEEK_SECONDS = 7 * 24 * 3600
MONTH_SECONDS = 30 * 24 * 3600
WEEKLY_ROLLOVER_SECONDS = 25 * 3600  # 25 hours/week (Scheme A rollover)
MONTHLY_TRANSCRIBE_SECONDS = 7_200 * 60
MONTHLY_LLM_MILLIPOINTS = 100_000  # 100 displayed points; 1 point = 1,000 milli-points
EXCHANGE_MILLIPOINTS_PER_HOUR = 10_000  # 10 AI points = 1 hour transcription
VALID_TIERS = ("lite", "pro", "max")
_TIER_RANK = {"lite": 1, "pro": 2, "max": 3}

# Concurrent per-request connections (threadpool charges, sync routes) contend
# on SQLite's write lock; without a busy timeout a contended writer fails
# immediately with "database is locked" (library/relay-dedup-design.md).
# 5000ms is orders of magnitude beyond class-scale write latency. Set
# explicitly so the guarantee is pinned in connect(), not inherited from the
# sqlite3 library default. WAL was evaluated and deferred: the default
# journal plus this timeout meets the pinned behavior without changing
# on-disk artifacts (deployment can still switch journal modes itself).
BUSY_TIMEOUT_MS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    transcribe_seconds_remaining INTEGER NOT NULL DEFAULT 0,
    monthly_transcribe_seconds INTEGER NOT NULL DEFAULT 432000,
    llm_millipoints_remaining INTEGER NOT NULL DEFAULT 100000,
    monthly_llm_millipoints INTEGER NOT NULL DEFAULT 100000,
    period_started_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    code TEXT PRIMARY KEY,
    transcribe_seconds INTEGER NOT NULL,
    llm_millipoints INTEGER NOT NULL DEFAULT 0,
    redeemed_by INTEGER REFERENCES accounts(id),
    redeemed_at REAL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    operation TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    millipoints INTEGER NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS campus_bindings (
    campus_id_hash TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    rewarded_at REAL
);
CREATE TABLE IF NOT EXISTS gift_links (
    token TEXT PRIMARY KEY,
    tier TEXT,
    duration_days INTEGER NOT NULL DEFAULT 0,
    basic_seconds INTEGER NOT NULL DEFAULT 0,
    advanced_seconds INTEGER NOT NULL DEFAULT 0,
    weekly_rollover_seconds INTEGER NOT NULL DEFAULT 0,
    llm_millipoints INTEGER NOT NULL DEFAULT 0,
    max_claims INTEGER NOT NULL DEFAULT 1,
    claimed_count INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    expires_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gift_claims (
    token TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    claimed_at REAL NOT NULL,
    PRIMARY KEY (token, account_id)
);
"""


def extract_token_from_input(raw: str) -> str:
    """Extract a gift/invite/redeem code from a raw string or full URL."""
    text = (raw or "").strip()
    if not text:
        return ""
    match = re.search(r"/(?:gift|invite|redeem)/([A-Za-z0-9_-]+)", text)
    if match:
        return match.group(1).strip().upper()
    return text.upper()


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (and initialize) the accounts database.

    Also the migration hook for new tables: the transcript reuse store
    gains its table here, idempotently, so deployed databases upgrade on
    their first connect after the feature lands.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.executescript(SCHEMA)
    _migrate_accounts(conn)
    conn.execute("INSERT OR IGNORE INTO campus_bindings(campus_id_hash, account_id, rewarded_at)"
                 " SELECT campus_id_hash, id, invite_rewarded_at FROM accounts WHERE campus_id_hash IS NOT NULL")
    _migrate_codes(conn)
    # Deferred import: product_auth imports accounts for constants.
    from . import product_auth

    product_auth.ensure_product_auth_schema(conn)
    transcript_store.ensure_schema(conn)
    ops_metrics.ensure_schema(conn)
    from . import direct_upload
    direct_upload.ensure_schema(conn)
    from . import llm_jobs
    llm_jobs.ensure_schema(conn)
    from . import account_evidence, catalog
    catalog.ensure_schema(conn)
    account_evidence.ensure_schema(conn)
    from . import account_profiles
    account_profiles.ensure_schema(conn)
    conn.commit()
    return conn


def _migrate_accounts(conn: sqlite3.Connection) -> None:
    """Add subscription, tier, dual-quota, rollover, and invite columns idempotently."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    columns = {
        "monthly_transcribe_seconds": "INTEGER NOT NULL DEFAULT 432000",
        "llm_millipoints_remaining": "INTEGER NOT NULL DEFAULT 100000",
        "monthly_llm_millipoints": "INTEGER NOT NULL DEFAULT 100000",
        "period_started_at": "REAL NOT NULL DEFAULT 0",
        "tier": "TEXT NOT NULL DEFAULT 'pro'",
        "tier_expires_at": "REAL NOT NULL DEFAULT 0",
        "basic_seconds_remaining": "INTEGER NOT NULL DEFAULT 0",
        "advanced_seconds_remaining": "INTEGER NOT NULL DEFAULT 0",
        "weekly_rollover_seconds": "INTEGER NOT NULL DEFAULT 0",
        "last_weekly_refill_at": "REAL NOT NULL DEFAULT 0",
        "invite_code": "TEXT",
        "invited_by": "INTEGER",
        "invite_rewarded_at": "REAL",
        "campus_id_hash": "TEXT",
        "campus_id_masked": "TEXT",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS accounts_invite_code_unique"
        " ON accounts(invite_code) WHERE invite_code IS NOT NULL"
    )


def _migrate_codes(conn: sqlite3.Connection) -> None:
    """Add an opt-in LLM grant without valuing existing codes."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(codes)")}
    if "llm_millipoints" not in existing:
        conn.execute(
            "ALTER TABLE codes ADD COLUMN llm_millipoints"
            " INTEGER NOT NULL DEFAULT 0"
        )


def ensure_invite_code(conn: sqlite3.Connection, account_id: int) -> str:
    """Return or lazily assign a unique invite code (PKU-XXXXXXXX) for the account."""
    row = conn.execute(
        "SELECT invite_code FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return ""
    if row["invite_code"]:
        return str(row["invite_code"])
    for _ in range(10):
        candidate = f"PKU-{secrets.token_hex(4).upper()}"
        try:
            with conn:
                conn.execute(
                    "UPDATE accounts SET invite_code = ? WHERE id = ? AND invite_code IS NULL",
                    (candidate, account_id),
                )
            check = conn.execute(
                "SELECT invite_code FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if check and check["invite_code"]:
                return str(check["invite_code"])
        except sqlite3.IntegrityError:
            continue
    return ""


def create_account(conn: sqlite3.Connection, label: str) -> int:
    now = time.time()
    invite_code = f"PKU-{secrets.token_hex(4).upper()}"
    cur = conn.execute(
        "INSERT INTO accounts (label, transcribe_seconds_remaining,"
        " monthly_transcribe_seconds, llm_millipoints_remaining,"
        " monthly_llm_millipoints, period_started_at, created_at, invite_code)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            label,
            0,
            MONTHLY_TRANSCRIBE_SECONDS,
            MONTHLY_LLM_MILLIPOINTS,
            MONTHLY_LLM_MILLIPOINTS,
            now,
            now,
            invite_code,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def issue_session(
    conn: sqlite3.Connection, account_id: int, ttl: float = SESSION_TTL_SECONDS
) -> str:
    """A fresh opaque session token for the app to store locally."""
    token = secrets.token_hex(24)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (token, account_id, issued_at, expires_at)"
        " VALUES (?, ?, ?, ?)",
        (token, account_id, now, now + ttl),
    )
    conn.commit()
    return token


def validate_session(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """The account behind a live session, or None (unknown/expired/disabled)."""
    return conn.execute(
        "SELECT a.* FROM sessions s JOIN accounts a ON a.id = s.account_id"
        " WHERE s.token = ? AND s.expires_at > ? AND a.status = 'active'",
        (token, time.time()),
    ).fetchone()


def create_code(
    conn: sqlite3.Connection,
    transcribe_seconds: int = MONTHLY_TRANSCRIBE_SECONDS,
    *,
    llm_millipoints: int = 0,
) -> str:
    """Create a single-use code; LLM value is always explicit and opt-in."""
    if llm_millipoints < 0:
        raise ValueError("llm_millipoints must be non-negative")
    code = secrets.token_hex(4).upper()
    conn.execute(
        "INSERT INTO codes (code, transcribe_seconds, llm_millipoints)"
        " VALUES (?, ?, ?)",
        (code, transcribe_seconds, llm_millipoints),
    )
    conn.commit()
    return code


@contextmanager
def _write_transaction(conn):
    if conn.in_transaction:
        yield
    else:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            yield


def effective_tier(row) -> str:
    tier = str(row["tier"] or "lite").lower()
    expiry = float(row["tier_expires_at"] or 0)
    return tier if tier in VALID_TIERS and (expiry == 0 or expiry > time.time()) else "lite"


def create_gift_link(
    conn: sqlite3.Connection,
    *,
    tier: str | None = None,
    duration_days: int = 30,
    basic_seconds: int = 0,
    advanced_seconds: int = 0,
    weekly_rollover_seconds: int = 0,
    llm_millipoints: int = 0,
    max_claims: int = 1,
    note: str = "",
    expires_at: float | None = None,
) -> str:
    """Create a shareable Gift Link token (GIFT-XXXXXXXXXXXX)."""
    if tier is not None and tier not in VALID_TIERS:
        raise ValueError(f"tier must be one of {VALID_TIERS}")
    if basic_seconds < 0 or advanced_seconds < 0 or weekly_rollover_seconds < 0 or llm_millipoints < 0:
        raise ValueError("gift grants must be non-negative")
    if duration_days < 0:
        raise ValueError("duration_days must be non-negative")
    if max_claims < 1:
        raise ValueError("max_claims must be >= 1")
    token = f"GIFT-{secrets.token_hex(6).upper()}"
    now = time.time()
    conn.execute(
        "INSERT INTO gift_links (token, tier, duration_days, basic_seconds,"
        " advanced_seconds, weekly_rollover_seconds, llm_millipoints,"
        " max_claims, claimed_count, note, expires_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (
            token,
            tier,
            int(duration_days),
            int(basic_seconds),
            int(advanced_seconds),
            int(weekly_rollover_seconds),
            int(llm_millipoints),
            int(max_claims),
            note,
            expires_at,
            now,
        ),
    )
    conn.commit()
    return token


def get_gift_link(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    normalized = extract_token_from_input(token)
    if not normalized:
        return None
    return conn.execute(
        "SELECT * FROM gift_links WHERE token = ?", (normalized,)
    ).fetchone()


def claim_gift_link(conn: sqlite3.Connection, token: str, account_id: int) -> bool:
    """Atomically claim a gift link for account_id."""
    normalized = extract_token_from_input(token)
    if not normalized:
        return False
    now = time.time()
    with _write_transaction(conn):
        refresh_monthly_quota(conn, account_id)
        _normalize_buckets(conn, account_id)
        gift = conn.execute(
            "SELECT * FROM gift_links WHERE token = ?", (normalized,)
        ).fetchone()
        if gift is None:
            return False
        if gift["expires_at"] is not None and float(gift["expires_at"]) <= now:
            return False
        already = conn.execute(
            "SELECT 1 FROM gift_claims WHERE token = ? AND account_id = ?",
            (normalized, account_id),
        ).fetchone()
        if already is not None:
            return False
        claimed = conn.execute(
            "UPDATE gift_links SET claimed_count = claimed_count + 1"
            " WHERE token = ? AND claimed_count < max_claims",
            (normalized,),
        )
        if claimed.rowcount != 1:
            return False
        conn.execute(
            "INSERT INTO gift_claims (token, account_id, claimed_at) VALUES (?, ?, ?)",
            (normalized, account_id, now),
        )
        acct = conn.execute(
            "SELECT tier, tier_expires_at, weekly_rollover_seconds, last_weekly_refill_at"
            " FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if acct is None:
            raise sqlite3.IntegrityError("account not found")
        current_tier = effective_tier(acct) if float(acct["tier_expires_at"] or 0) > now else "lite"
        gift_tier = (gift["tier"] or "").lower()
        new_tier = current_tier
        if gift_tier in _TIER_RANK and _TIER_RANK[gift_tier] >= _TIER_RANK.get(current_tier, 1):
            new_tier = gift_tier
        current_exp = float(acct["tier_expires_at"] or 0.0)
        duration_days = int(gift["duration_days"] or 0)
        grants_membership = gift_tier in VALID_TIERS or int(gift["weekly_rollover_seconds"] or 0) > 0
        new_exp = (max(current_exp, now) + duration_days * 86400) if duration_days > 0 and grants_membership else current_exp
        new_rollover = max(int(acct["weekly_rollover_seconds"] or 0), int(gift["weekly_rollover_seconds"] or 0))
        last_refill = float(acct["last_weekly_refill_at"] or 0.0)
        if new_rollover > 0 and (last_refill <= 0 or current_exp <= now):
            last_refill = now
        basic_add = int(gift["basic_seconds"] or 0)
        adv_add = int(gift["advanced_seconds"] or 0)
        total_add = basic_add + adv_add
        llm_add = int(gift["llm_millipoints"] or 0)
        conn.execute(
            "UPDATE accounts SET"
            " tier = ?,"
            " tier_expires_at = ?,"
            " weekly_rollover_seconds = ?,"
            " last_weekly_refill_at = ?,"
            " basic_seconds_remaining = basic_seconds_remaining + ?,"
            " advanced_seconds_remaining = advanced_seconds_remaining + ?,"
            " transcribe_seconds_remaining = transcribe_seconds_remaining + ?,"
            " llm_millipoints_remaining = llm_millipoints_remaining + ?"
            " WHERE id = ?",
            (
                new_tier,
                new_exp,
                new_rollover,
                last_refill,
                basic_add,
                adv_add,
                total_add,
                llm_add,
                account_id,
            ),
        )
    return True


def _grant_invite_lite_month(conn: sqlite3.Connection, target_account_id: int, now: float) -> None:
    """Grant 1 month of Lite (25h/week rollover + immediate first-week 25h basic transcription)."""
    refresh_monthly_quota(conn, target_account_id)
    _normalize_buckets(conn, target_account_id)
    row = conn.execute(
        "SELECT tier, tier_expires_at, weekly_rollover_seconds, last_weekly_refill_at"
        " FROM accounts WHERE id = ?",
        (target_account_id,),
    ).fetchone()
    if row is None:
        return
    current_exp = float(row["tier_expires_at"] or 0.0)
    new_exp = max(current_exp, now) + MONTH_SECONDS
    new_rollover = max(int(row["weekly_rollover_seconds"] or 0), WEEKLY_ROLLOVER_SECONDS)
    last_refill = float(row["last_weekly_refill_at"] or 0.0)
    if last_refill <= 0 or current_exp <= now:
        last_refill = now
    conn.execute(
        "UPDATE accounts SET"
        " basic_seconds_remaining = basic_seconds_remaining + ?,"
        " transcribe_seconds_remaining = transcribe_seconds_remaining + ?,"
        " weekly_rollover_seconds = ?,"
        " last_weekly_refill_at = ?,"
        " tier_expires_at = ?, tier = ?"
        " WHERE id = ?",
        (
            WEEKLY_ROLLOVER_SECONDS,
            WEEKLY_ROLLOVER_SECONDS,
            new_rollover,
            last_refill,
            new_exp,
            effective_tier(row) if current_exp > now else "lite",
            target_account_id,
        ),
    )


def apply_invite_code(conn: sqlite3.Connection, account_id: int, raw_code: str) -> bool:
    """Bind an inviter to account_id via invite code; rewards trigger once campus ID is bound."""
    normalized = extract_token_from_input(raw_code)
    if not normalized:
        return False
    now = time.time()
    with _write_transaction(conn):
        inviter = conn.execute(
            "SELECT id FROM accounts WHERE invite_code = ?", (normalized,)
        ).fetchone()
        if inviter is None:
            return False
        inviter_id = int(inviter["id"])
        if inviter_id == account_id:
            return False
        acct = conn.execute(
            "SELECT invited_by, invite_rewarded_at, campus_id_hash FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if acct is None or acct["invited_by"] is not None:
            return False
        conn.execute(
            "UPDATE accounts SET invited_by = ? WHERE id = ?",
            (inviter_id, account_id),
        )
        reward_claimed = conn.execute(
            "UPDATE campus_bindings SET rewarded_at = ? WHERE campus_id_hash = ?"
            " AND account_id = ? AND rewarded_at IS NULL",
            (now, acct["campus_id_hash"], account_id),
        ).rowcount if acct["invite_rewarded_at"] is None else 0
        if reward_claimed:
            _grant_invite_lite_month(conn, account_id, now)
            _grant_invite_lite_month(conn, inviter_id, now)
            conn.execute(
                "UPDATE accounts SET invite_rewarded_at = ? WHERE id = ?",
                (now, account_id),
            )
    return True


class CampusBindingConflict(ValueError):
    """The account is already bound to another campus identity."""


def bind_campus_account(
    conn: sqlite3.Connection, account_id: int, student_id: str
) -> dict:
    """Bind a PKU campus student ID (hashed) to account_id and grant invite rewards if eligible."""
    cleaned = (student_id or "").strip().lower()
    if not cleaned or len(cleaned) < 4 or len(cleaned) > 64:
        raise ValueError("请输入有效的教学网账号（学号）")
    campus_hash = hashlib.sha256(f"pku-campus-v1:{cleaned}".encode("utf-8")).hexdigest()
    now = time.time()
    with _write_transaction(conn):
        acct = conn.execute(
            "SELECT invited_by, invite_rewarded_at, campus_id_hash, campus_id_masked FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if acct is not None and acct["campus_id_hash"] and acct["campus_id_hash"] != campus_hash:
            bound = acct["campus_id_masked"] or "原学号（历史绑定未保存学号尾号）"
            raise CampusBindingConflict(f"当前账号已绑定教学网学号 {bound}，请使用该学号连接；如需更换，请联系支持。")
        existing_other = conn.execute(
            "SELECT account_id FROM campus_bindings WHERE campus_id_hash = ? AND account_id != ?",
            (campus_hash, account_id),
        ).fetchone()
        if existing_other is not None:
            return {
                "campus_bound": False,
            "invite_pending_campus_reward": False,
                "reward_granted": False,
                "already_used": True,
                "reward_seconds": 0,
            }
        if acct is None:
            return {
                "campus_bound": False,
            "invite_pending_campus_reward": False,
                "reward_granted": False,
                "already_used": False,
                "reward_seconds": 0,
            }
        conn.execute(
            "INSERT OR IGNORE INTO campus_bindings(campus_id_hash, account_id) VALUES (?, ?)",
            (campus_hash, account_id),
        )
        conn.execute(
            "UPDATE accounts SET campus_id_hash = ?, campus_id_masked = ? WHERE id = ?",
            (campus_hash, cleaned[:2] + "****" + cleaned[-4:] if len(cleaned) > 6 else "****" + cleaned[-2:], account_id),
        )
        reward_granted = False
        reward_seconds = 0
        if acct["invited_by"] is not None and acct["invite_rewarded_at"] is None:
            claimed = conn.execute(
                "UPDATE campus_bindings SET rewarded_at = ? WHERE campus_id_hash = ? AND rewarded_at IS NULL",
                (now, campus_hash),
            ).rowcount
            if not claimed:
                return {"campus_bound": True, "reward_granted": False, "already_used": True, "reward_seconds": 0}
            inviter_id = int(acct["invited_by"])
            _grant_invite_lite_month(conn, account_id, now)
            _grant_invite_lite_month(conn, inviter_id, now)
            conn.execute(
                "UPDATE accounts SET invite_rewarded_at = ? WHERE id = ?",
                (now, account_id),
            )
            reward_granted = True
            reward_seconds = WEEKLY_ROLLOVER_SECONDS
    return {
        "campus_bound": True,
        "reward_granted": reward_granted,
        "already_used": False,
        "reward_seconds": reward_seconds,
    }


class InsufficientPoints(ValueError):
    """The requested exchange exceeds the available AI points."""


def exchange_points_for_seconds(
    conn: sqlite3.Connection,
    account_id: int,
    hours: int = 1,
    *,
    target: str = "advanced",
) -> dict:
    """Exchange AI points for transcription time (10 AI points = 1 hour)."""
    if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1 or hours > 100:
        raise ValueError("兑换小时数需为 1~100 的整数")
    if target not in ("basic", "advanced"):
        raise ValueError("target must be basic or advanced")
    refresh_monthly_quota(conn, account_id)
    cost_millipoints = hours * EXCHANGE_MILLIPOINTS_PER_HOUR
    gain_seconds = hours * 3600
    bucket_col = "basic_seconds_remaining" if target == "basic" else "advanced_seconds_remaining"
    with _write_transaction(conn):
        _normalize_buckets(conn, account_id)
        updated = conn.execute(
            f"UPDATE accounts SET"
            f" llm_millipoints_remaining = llm_millipoints_remaining - ?,"
            f" {bucket_col} = {bucket_col} + ?,"
            f" transcribe_seconds_remaining = transcribe_seconds_remaining + ?"
            f" WHERE id = ? AND llm_millipoints_remaining >= ?",
            (cost_millipoints, gain_seconds, gain_seconds, account_id, cost_millipoints),
        )
        if updated.rowcount != 1:
            raise InsufficientPoints("AI 点数不足（每兑换 1 小时转写需 10 AI 点）")
    return {**quota_breakdown(conn, account_id), "points_spent": hours * 10,
            "seconds_added": gain_seconds, "target": target}


def activate_code(conn: sqlite3.Connection, code: str) -> tuple[str, int] | None:
    """Consume a code and create its first platform account/session atomically."""
    normalized = extract_token_from_input(code)
    now = time.time()
    with conn:
        claimed = conn.execute(
            "UPDATE codes SET redeemed_at = ? WHERE code = ? AND redeemed_by IS NULL",
            (now, normalized),
        )
        if claimed.rowcount != 1:
            return None
        seconds = conn.execute(
            "SELECT transcribe_seconds FROM codes WHERE code = ?", (normalized,)
        ).fetchone()["transcribe_seconds"]
        label = f"用户-{secrets.token_hex(6)}"
        invite_code = f"PKU-{secrets.token_hex(4).upper()}"
        cur = conn.execute(
            "INSERT INTO accounts (label, transcribe_seconds_remaining,"
            " basic_seconds_remaining, advanced_seconds_remaining,"
            " monthly_transcribe_seconds, llm_millipoints_remaining,"
            " monthly_llm_millipoints, period_started_at, created_at, invite_code)"
            " VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?)",
            (
                label,
                seconds,
                seconds,
                seconds,
                MONTHLY_LLM_MILLIPOINTS,
                MONTHLY_LLM_MILLIPOINTS,
                now,
                now,
                invite_code,
            ),
        )
        account_id = int(cur.lastrowid)
        token = secrets.token_hex(24)
        conn.execute(
            "INSERT INTO sessions (token, account_id, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (token, account_id, now, now + SESSION_TTL_SECONDS),
        )
        conn.execute(
            "UPDATE codes SET redeemed_by = ?, redeemed_at = ? WHERE code = ?",
            (account_id, now, normalized),
        )
    return token, int(seconds)


def redeem_code(conn: sqlite3.Connection, code: str, account_id: int) -> bool:
    """Atomically redeem a Gift Link, Invite Link/Code, or legacy single-use code."""
    normalized = extract_token_from_input(code)
    if not normalized:
        return False
    if normalized.startswith("GIFT-") or get_gift_link(conn, normalized) is not None:
        return claim_gift_link(conn, normalized, account_id)
    if normalized.startswith("PKU-"):
        return apply_invite_code(conn, account_id, normalized)
    now = time.time()
    with conn:
        claimed = conn.execute(
            "UPDATE codes SET redeemed_by = ?, redeemed_at = ?"
            " WHERE code = ? AND redeemed_by IS NULL",
            (account_id, now, normalized),
        )
        if claimed.rowcount != 1:
            return False
        grant = conn.execute(
            "SELECT transcribe_seconds, llm_millipoints FROM codes WHERE code = ?",
            (normalized,),
        ).fetchone()
        conn.execute(
            "UPDATE accounts SET transcribe_seconds_remaining ="
            " transcribe_seconds_remaining + ?,"
            " advanced_seconds_remaining = advanced_seconds_remaining + ?,"
            " llm_millipoints_remaining = llm_millipoints_remaining + ?"
            " WHERE id = ?",
            (
                grant["transcribe_seconds"],
                grant["transcribe_seconds"],
                grant["llm_millipoints"],
                account_id,
            ),
        )
    return True


def record_transcription(
    conn: sqlite3.Connection,
    account_id: int,
    seconds: int,
    *,
    operation_id=None,
    mode: str = "basic",
) -> None:
    """Meter actual usage; advanced seconds automatically cover basic 1:1 when basic is exhausted."""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        raise ValueError("seconds must be a non-negative integer")
    if mode not in ("basic", "advanced"):
        raise ValueError("mode must be basic or advanced")
    with _write_transaction(conn):
        _normalize_buckets(conn, account_id)
        row = conn.execute("SELECT basic_seconds_remaining FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise ValueError("account not found")
        use_basic = min(max(row[0], 0), seconds) if mode == "basic" else 0
        conn.execute(
            "UPDATE accounts SET transcribe_seconds_remaining = transcribe_seconds_remaining - ?,"
            " basic_seconds_remaining = basic_seconds_remaining - ?,"
            " advanced_seconds_remaining = advanced_seconds_remaining - ? WHERE id = ?",
            (seconds, use_basic, seconds - use_basic, account_id),
        )
        conn.execute(
            "INSERT INTO usage(account_id, kind, seconds, recorded_at, operation_id) VALUES (?, 'transcribe', ?, ?, ?)",
            (account_id, seconds, time.time(), operation_id),
        )


def _normalize_buckets(conn, account_id):
    # Existing installations and operator tooling may still grant the total alone.
    for tier_test, bucket in (("tier = 'lite'", "basic_seconds_remaining"),
                              ("tier != 'lite'", "advanced_seconds_remaining")):
        conn.execute(
            f"UPDATE accounts SET {bucket} = {bucket} + "
            "(transcribe_seconds_remaining - basic_seconds_remaining - advanced_seconds_remaining) "
            f"WHERE id = ? AND {tier_test} AND transcribe_seconds_remaining > basic_seconds_remaining + advanced_seconds_remaining",
            (account_id,),
        )


def refresh_monthly_quota(conn: sqlite3.Connection, account_id: int) -> None:
    """Apply weekly rollover refill (Scheme A) when configured, else legacy 30-day reset."""
    with _write_transaction(conn):
        row = conn.execute(
            "SELECT period_started_at, weekly_rollover_seconds, last_weekly_refill_at,"
            " tier, tier_expires_at FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            return
        now = time.time()
        weekly_rollover = int(row["weekly_rollover_seconds"] or 0)
        last_refill = float(row["last_weekly_refill_at"] or 0.0)
        if weekly_rollover > 0:
            if last_refill <= 0:
                return
            tier_exp = float(row["tier_expires_at"] or 0.0)
            effective_now = min(now, tier_exp)
            if effective_now - last_refill >= WEEK_SECONDS:
                weeks = int((effective_now - last_refill) // WEEK_SECONDS)
                if weeks > 0:
                    added = weeks * weekly_rollover
                    new_last = last_refill + weeks * WEEK_SECONDS
                    tier = (row["tier"] or "lite").lower()
                    bucket_col = (
                        "advanced_seconds_remaining"
                        if tier in ("pro", "max")
                        else "basic_seconds_remaining"
                    )
                    conn.execute(
                        f"UPDATE accounts SET"
                        f" {bucket_col} = {bucket_col} + ?,"
                        f" transcribe_seconds_remaining = transcribe_seconds_remaining + ?,"
                        f" last_weekly_refill_at = ?,"
                        f" period_started_at = ?"
                        f" WHERE id = ?",
                        (added, added, new_last, now, account_id),
                    )

            return

        if now - row["period_started_at"] < MONTH_SECONDS:
            return
        conn.execute(
            "UPDATE accounts SET transcribe_seconds_remaining = monthly_transcribe_seconds,"
            " basic_seconds_remaining = 0, advanced_seconds_remaining = monthly_transcribe_seconds,"
            " llm_millipoints_remaining = monthly_llm_millipoints, period_started_at = ?"
            " WHERE id = ?",
            (now, account_id),
        )



def remaining_seconds(conn: sqlite3.Connection, account_id: int) -> int:
    refresh_monthly_quota(conn, account_id)
    row = conn.execute(
        "SELECT transcribe_seconds_remaining FROM accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    return int(row["transcribe_seconds_remaining"]) if row else 0


def remaining_llm_millipoints(conn: sqlite3.Connection, account_id: int) -> int:
    refresh_monthly_quota(conn, account_id)
    row = conn.execute(
        "SELECT llm_millipoints_remaining FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    return int(row["llm_millipoints_remaining"]) if row else 0


def quota_breakdown(conn: sqlite3.Connection, account_id: int) -> dict:
    """Return full tier, dual transcription seconds, weekly rollover, and invite info."""
    refresh_monthly_quota(conn, account_id)
    invite_code = ensure_invite_code(conn, account_id)
    row = conn.execute(
        "SELECT transcribe_seconds_remaining, basic_seconds_remaining,"
        " advanced_seconds_remaining, weekly_rollover_seconds, tier,"
        " tier_expires_at, llm_millipoints_remaining, campus_id_hash, campus_id_masked, invited_by, invite_rewarded_at"
        " FROM accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return {
            "tier": "lite",
            "transcribe_seconds_remaining": 0,
            "basic_seconds_remaining": 0,
            "advanced_seconds_remaining": 0,
            "weekly_rollover_seconds": 0,
            "llm_points_remaining": 0.0,
            "invite_code": "",
            "campus_bound": False,
            "invite_pending_campus_reward": False,
        }
    total_sec = int(row["transcribe_seconds_remaining"] or 0)
    bas_sec = max(int(row["basic_seconds_remaining"] or 0), 0)
    adv_sec = max(int(row["advanced_seconds_remaining"] or 0), 0)
    tier = effective_tier(row)
    if bas_sec + adv_sec < total_sec:
        diff = total_sec - (bas_sec + adv_sec)
        if tier in ("pro", "max"):
            adv_sec += diff
        else:
            bas_sec += diff
    return {
        "tier": tier,
        "tier_expires_at": float(row["tier_expires_at"] or 0),
        "transcribe_seconds_remaining": total_sec,
        "basic_seconds_remaining": bas_sec,
        "advanced_seconds_remaining": adv_sec,
        "weekly_rollover_seconds": int(row["weekly_rollover_seconds"] or 0),
        "llm_points_remaining": int(row["llm_millipoints_remaining"] or 0) / 1000,
        "invite_code": invite_code,
        "campus_bound": bool(row["campus_id_hash"]),
        "campus_id_masked": row["campus_id_masked"] or "",
        "invite_pending_campus_reward": bool(row["invited_by"] is not None and row["invite_rewarded_at"] is None),
    }


def is_bundled_llm_operation(
    conn: sqlite3.Connection, account_id: int, operation: str
) -> bool:
    """True when lecture notes / notification organization is bundled with active transcription/tier entitlement."""
    if operation not in ("notes", "summary", "qa"):
        return False
    refresh_monthly_quota(conn, account_id)
    row = conn.execute(
        "SELECT weekly_rollover_seconds, basic_seconds_remaining,"
        " advanced_seconds_remaining, tier_expires_at, transcribe_seconds_remaining"
        " FROM accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return False
    if (int(row["transcribe_seconds_remaining"] or 0) > 0
            or int(row["basic_seconds_remaining"] or 0) + int(row["advanced_seconds_remaining"] or 0) > 0):
        return True
    if float(row["tier_expires_at"] or 0.0) > time.time():
        return True
    return False


def record_llm(
    conn: sqlite3.Connection,
    account_id: int,
    operation: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    millipoints: int,
) -> bool:
    """Atomically charge a completed LLM call; false means exhausted."""
    with conn:
        if millipoints > 0:
            updated = conn.execute(
                "UPDATE accounts SET llm_millipoints_remaining = llm_millipoints_remaining - ?"
                " WHERE id = ? AND llm_millipoints_remaining >= ?",
                (millipoints, account_id, millipoints),
            )
            if updated.rowcount != 1:
                return False
        else:
            exists = conn.execute(
                "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if exists is None:
                return False
        conn.execute(
            "INSERT INTO llm_usage (account_id, operation, model, input_tokens,"
            " output_tokens, millipoints, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, operation, model, input_tokens, output_tokens, millipoints, time.time()),
        )
    return True
