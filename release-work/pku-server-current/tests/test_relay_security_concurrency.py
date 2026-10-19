"""Auth/quota hardening under genuinely overlapping requests (shared loop).

Pins the concurrency contract (VAL-SECU-003/008/010): the /v1/llm
post-call charge is atomic — two overlapping calls with balance for
exactly one produce one 200 and one 402 with final balance 0; ten
overlapping activations of one code produce exactly one account and
one session; overlapping redemptions of one code credit the value
exactly once.

Harness rule (library/user-testing.md, binding): every route-level
test here drives REAL overlap through ONE shared event loop — a single
httpx.AsyncClient with ONE ASGITransport, every request launched via
asyncio.gather before any completes. The LLM race is forced and proven
by a barrier inside the fake llm_fn: both callers are inside the
vendor simultaneously, past the 402 pre-check, before either charge
lands.

The activation/redemption races are additionally pinned below the
routes, at the accounts layer, with a barrier-instrumented
connection: every thread rendezvouses at its first write statement
before SQLite takes any lock, so the pre-fix read-then-write race
fires deterministically and the post-fix transactional claim decides
the single winner — no timing luck anywhere.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading

import httpx

from server import accounts, relay
from server.llm import Completion

HARNESS_TIMEOUT_SECONDS = 60.0  # a leaked barrier/lock fails the test, never hangs it
RENDEZVOUS_TIMEOUT_SECONDS = 10.0
# The statements that make a connection a writer (the sqlite3 module
# opens the implicit write transaction on the first of these).
_WRITE_STATEMENT = re.compile(r"^\s*(UPDATE|INSERT|DELETE|REPLACE)\b", re.IGNORECASE)


# --- fixtures: provisioned accounts, one shared-loop POST gather ---


def make_security_app(tmp_path, llm_fn=None, provisioned=(("同学A", 600), ("同学B", 600))):
    """A fresh DB + app with provisioned accounts; returns (app, {label: token})."""
    conn = accounts.connect(tmp_path / "accounts.db")
    tokens = {}
    for label, quota in provisioned:
        account_id = accounts.create_account(conn, label)
        conn.execute(
            "UPDATE accounts SET transcribe_seconds_remaining = ? WHERE id = ?",
            (quota, account_id),
        )
        conn.commit()
        tokens[label] = accounts.issue_session(conn, account_id)
    conn.close()
    app = relay.create_relay_app(tmp_path / "accounts.db", llm_fn=llm_fn)
    return app, tokens


async def _gather_posts(app, specs):
    """All requests in flight at once on ONE shared event loop.

    One AsyncClient + one ASGITransport for the entire test: the app
    runs directly on this loop, exactly as the binding harness pattern
    requires — no per-thread transports anywhere.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://relay.test"
    ) as client:

        async def post(spec):
            return await client.post(
                spec["path"], json=spec.get("json"), headers=spec.get("headers") or {}
            )

        return list(await asyncio.gather(*(post(spec) for spec in specs)))


def run_overlapping_posts(app, specs):
    """Drive the specs through the shared loop; bounded so bugs fail, not hang."""
    return asyncio.run(
        asyncio.wait_for(_gather_posts(app, specs), timeout=HARNESS_TIMEOUT_SECONDS)
    )


def scalar(tmp_path, query, params=()):
    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        return conn.execute(query, params).fetchone()[0]
    finally:
        conn.close()


def race_threads(worker, count):
    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()


# --- VAL-SECU-003: the /v1/llm post-call charge is atomic under overlap ---


class OverlappingLlm:
    """Fake llm_fn that parks every caller until all are in the vendor at once.

    The barrier sits between the 402 pre-check and the atomic charge, so
    every caller has already passed ``remaining > 0`` before any charge
    lands — the exact race VAL-SECU-003 pins. Thread ids record the
    genuine multi-threaded overlap.
    """

    def __init__(self, parties):
        self.calls = 0
        self.threads = []
        self.barrier = threading.Barrier(parties)

    def __call__(self, operation, system, user):
        self.calls += 1
        self.threads.append(threading.get_ident())
        self.barrier.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS)
        return Completion("内容", 100, 20, 7, "qwen3.5-flash")


def test_overlapping_llm_calls_with_balance_for_one_one_200_one_402(tmp_path):
    """Balance for exactly one call: the atomic charge keeps it one-or-the-other."""
    vendor = OverlappingLlm(parties=2)
    app, tokens = make_security_app(
        tmp_path, llm_fn=vendor, provisioned=(("同学A", 600),)
    )
    conn = accounts.connect(tmp_path / "accounts.db")
    conn.execute("UPDATE accounts SET llm_millipoints_remaining = 7 WHERE label = '同学A'")
    conn.commit()
    conn.close()

    spec = {
        "path": "/v1/llm",
        "json": {"operation": "grade", "system": "系统", "user": "问题"},
        "headers": {"Authorization": f"Bearer {tokens['同学A']}"},
    }
    responses = run_overlapping_posts(app, [spec, dict(spec)])

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 402]  # one completes, one is refused
    assert vendor.calls == 2  # both genuinely reached the vendor
    assert len(set(vendor.threads)) == 2  # on two distinct threads, simultaneously

    ok = next(r for r in responses if r.status_code == 200).json()
    assert ok == {"content": "内容", "points_charged": 0.007}
    refused = next(r for r in responses if r.status_code == 402).json()
    assert refused["detail"] == "AI points exhausted"

    # Final balance is exactly 0 — the refused charge never partially decremented.
    assert (
        scalar(
            tmp_path,
            "SELECT llm_millipoints_remaining FROM accounts WHERE label = '同学A'",
        )
        == 0
    )
    # Exactly one usage row, for the one completed call.
    assert scalar(tmp_path, "SELECT COUNT(*) FROM llm_usage") == 1
    assert scalar(tmp_path, "SELECT millipoints FROM llm_usage") == 7


# --- VAL-SECU-008: ten overlapping activations of the same code ---


def test_ten_overlapping_activations_create_exactly_one_account_and_session(tmp_path):
    conn = accounts.connect(tmp_path / "accounts.db")
    code = accounts.create_code(conn, 720)
    conn.close()
    app = relay.create_relay_app(tmp_path / "accounts.db")

    responses = run_overlapping_posts(
        app, [{"path": "/v1/activate", "json": {"code": code}} for _ in range(10)]
    )

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1  # exactly one winner
    assert statuses.count(400) == 9

    assert scalar(tmp_path, "SELECT COUNT(*) FROM accounts") == 1
    assert scalar(tmp_path, "SELECT COUNT(*) FROM sessions") == 1

    winner = next(
        response for response in responses if response.status_code == 200
    ).json()
    assert winner["transcribe_seconds_remaining"] == 720

    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        account = accounts.validate_session(conn, winner["platform_token"])
        code_row = conn.execute(
            "SELECT redeemed_by FROM codes WHERE code = ?", (code,)
        ).fetchone()
        assert account is not None  # the one session the activation created
        assert code_row["redeemed_by"] == account["id"]  # redeemed by that account
    finally:
        conn.close()


# --- VAL-SECU-010: overlapping redemptions of the same code ---


def test_overlapping_two_account_redemptions_credit_exactly_once(tmp_path):
    """Two accounts race one code; exactly one balance moves, by the code's value."""
    app, tokens = make_security_app(tmp_path, provisioned=(("同学A", 0), ("同学B", 0)))
    conn = accounts.connect(tmp_path / "accounts.db")
    code = accounts.create_code(conn, 3600)
    conn.close()

    responses = run_overlapping_posts(
        app,
        [
            {
                "path": "/v1/redeem",
                "json": {"code": code},
                "headers": {"Authorization": f"Bearer {tokens['同学A']}"},
            },
            {
                "path": "/v1/redeem",
                "json": {"code": code},
                "headers": {"Authorization": f"Bearer {tokens['同学B']}"},
            },
        ],
    )

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 400]  # one redemption wins

    # The winner holds exactly the code's value; the loser is unchanged.
    balances = sorted(
        scalar(
            tmp_path,
            "SELECT transcribe_seconds_remaining FROM accounts WHERE label = ?",
            (label,),
        )
        for label in ("同学A", "同学B")
    )
    assert balances == [0, 3600]


def test_overlapping_same_account_redemptions_credit_exactly_once(tmp_path):
    """The same account races itself: final balance is starting + value, once."""
    app, tokens = make_security_app(tmp_path, provisioned=(("同学A", 600),))
    conn = accounts.connect(tmp_path / "accounts.db")
    code = accounts.create_code(conn, 3600)
    conn.close()

    spec = {
        "path": "/v1/redeem",
        "json": {"code": code},
        "headers": {"Authorization": f"Bearer {tokens['同学A']}"},
    }
    responses = run_overlapping_posts(app, [spec, dict(spec)])

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 400]
    assert (
        scalar(
            tmp_path,
            "SELECT transcribe_seconds_remaining FROM accounts WHERE label = '同学A'",
        )
        == 600 + 3600
    )  # credited exactly once


# --- the race window itself, forced at the accounts layer (deterministic) ---


class _RendezvousConnection(sqlite3.Connection):
    """Test-only connection that forces the single-use race window.

    Each connection's FIRST write statement blocks on a shared Barrier
    until every racing thread has reached its own first write — in
    front of statement execution, before SQLite takes any lock, so
    nothing can deadlock. Pre-fix, that first write happens AFTER the
    redeemed_by read-check, so every thread has already seen
    ``redeemed_by IS NULL`` when the barrier releases them and the
    read-then-write race fires deterministically. Post-fix, the first
    write IS the transactional claim, so the barrier proves the claims
    genuinely overlap and the rowcount guard picks the single winner.
    """

    barrier: threading.Barrier  # set by the test before connecting

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rendezvous_fired = False
        self.row_factory = sqlite3.Row
        self.execute(f"PRAGMA busy_timeout = {accounts.BUSY_TIMEOUT_MS}")

    def execute(self, sql, *args, **kwargs):
        if not self._rendezvous_fired and _WRITE_STATEMENT.match(sql):
            self._rendezvous_fired = True
            self.barrier.wait(timeout=RENDEZVOUS_TIMEOUT_SECONDS)
        return super().execute(sql, *args, **kwargs)


def _rendezvous_connect(tmp_path, barrier):
    _RendezvousConnection.barrier = barrier
    return sqlite3.connect(tmp_path / "accounts.db", factory=_RendezvousConnection)


def test_redeem_race_forced_at_first_write_credits_exactly_once(tmp_path):
    """Both read-checks pass before either write commits — only a claim survives."""
    conn = accounts.connect(tmp_path / "accounts.db")
    account_id = accounts.create_account(conn, "同学A")
    code = accounts.create_code(conn, 3600)
    conn.close()

    barrier = threading.Barrier(2)
    results, errors = [], []

    def redeem_side():
        own = _rendezvous_connect(tmp_path, barrier)
        try:
            results.append(accounts.redeem_code(own, code, account_id))
        except BaseException as exc:  # pragma: no cover - surfaces harness faults
            errors.append(exc)
        finally:
            own.close()

    race_threads(redeem_side, 2)

    assert errors == []
    assert sorted(results) == [False, True]  # exactly one claim won

    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        assert accounts.remaining_seconds(conn, account_id) == 3600  # value, once
        code_row = conn.execute(
            "SELECT redeemed_by FROM codes WHERE code = ?", (code,)
        ).fetchone()
        assert code_row["redeemed_by"] == account_id
    finally:
        conn.close()


def test_activate_race_forced_at_first_write_creates_one_account_session(tmp_path):
    """Both read-checks pass before either write commits — one activation wins."""
    conn = accounts.connect(tmp_path / "accounts.db")
    code = accounts.create_code(conn, 720)
    conn.close()

    barrier = threading.Barrier(2)
    results, errors = [], []

    def activate_side():
        own = _rendezvous_connect(tmp_path, barrier)
        try:
            results.append(accounts.activate_code(own, code))
        except BaseException as exc:  # pragma: no cover - surfaces harness faults
            errors.append(exc)
        finally:
            own.close()

    race_threads(activate_side, 2)

    assert errors == []
    activated = [outcome for outcome in results if outcome is not None]
    assert len(activated) == 1  # exactly one activation won the code
    token, seconds = activated[0]
    assert seconds == 720

    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        account = accounts.validate_session(conn, token)
        assert account is not None
        code_row = conn.execute(
            "SELECT redeemed_by FROM codes WHERE code = ?", (code,)
        ).fetchone()
        assert code_row["redeemed_by"] == account["id"]
    finally:
        conn.close()
