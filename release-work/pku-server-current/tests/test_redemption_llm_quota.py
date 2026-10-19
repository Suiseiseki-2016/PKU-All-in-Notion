"""Dual-grant redemption codes and backwards-compatible migration coverage."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from server import accounts, relay
from server.llm import Completion


def _old_schema_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE accounts (
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
        CREATE TABLE codes (
            code TEXT PRIMARY KEY,
            transcribe_seconds INTEGER NOT NULL,
            redeemed_by INTEGER REFERENCES accounts(id),
            redeemed_at REAL
        );
    """)
    conn.execute(
        "INSERT INTO accounts (id, label, transcribe_seconds_remaining,"
        " llm_millipoints_remaining, created_at) VALUES (7, 'existing', 11, 13, 1)"
    )
    conn.execute(
        "INSERT INTO codes (code, transcribe_seconds, redeemed_by, redeemed_at)"
        " VALUES ('USED0001', 30, 7, 123.5)"
    )
    conn.execute(
        "INSERT INTO codes (code, transcribe_seconds, redeemed_by, redeemed_at)"
        " VALUES ('OPEN0001', 60, NULL, NULL)"
    )
    conn.commit()
    conn.close()


def _provision(db_path, *, seconds=0, llm_millipoints=0):
    conn = accounts.connect(db_path)
    account_id = accounts.create_account(conn, "student-a")
    conn.execute(
        "UPDATE accounts SET transcribe_seconds_remaining = ?,"
        " llm_millipoints_remaining = ? WHERE id = ?",
        (seconds, llm_millipoints, account_id),
    )
    conn.commit()
    token = accounts.issue_session(conn, account_id)
    conn.close()
    return account_id, token


def _balances(db_path, account_id):
    conn = accounts.connect(db_path)
    try:
        row = conn.execute(
            "SELECT transcribe_seconds_remaining, llm_millipoints_remaining"
            " FROM accounts WHERE id = ?", (account_id,),
        ).fetchone()
        return tuple(row)
    finally:
        conn.close()


def test_old_codes_schema_migrates_idempotently_without_financial_backfill(tmp_path):
    db_path = tmp_path / "old.db"
    _old_schema_db(db_path)
    for _ in range(2):
        accounts.connect(db_path).close()

    conn = accounts.connect(db_path)
    try:
        columns = {
            row["name"]: (row["type"], row["notnull"], row["dflt_value"])
            for row in conn.execute("PRAGMA table_info(codes)")
        }
        assert columns["llm_millipoints"] == ("INTEGER", 1, "0")
        assert dict(conn.execute(
            "SELECT code, transcribe_seconds, llm_millipoints, redeemed_by,"
            " redeemed_at FROM codes WHERE code = 'USED0001'"
        ).fetchone()) == {
            "code": "USED0001", "transcribe_seconds": 30, "llm_millipoints": 0,
            "redeemed_by": 7, "redeemed_at": 123.5,
        }
        assert conn.execute(
            "SELECT llm_millipoints FROM codes WHERE code = 'OPEN0001'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_existing_create_code_call_and_old_code_stay_transcription_only(tmp_path):
    db_path = tmp_path / "accounts.db"
    account_id, _ = _provision(db_path, seconds=5, llm_millipoints=7)
    conn = accounts.connect(db_path)
    code = accounts.create_code(conn, 60)
    row = conn.execute(
        "SELECT transcribe_seconds, llm_millipoints FROM codes WHERE code = ?", (code,)
    ).fetchone()
    assert tuple(row) == (60, 0)
    assert accounts.redeem_code(conn, code, account_id) is True
    assert accounts.remaining_seconds(conn, account_id) == 65
    assert accounts.remaining_llm_millipoints(conn, account_id) == 7
    conn.close()


def test_dual_grant_redeem_credits_both_once_and_keeps_response_shape(tmp_path):
    db_path = tmp_path / "accounts.db"
    account_id, token = _provision(db_path, seconds=5, llm_millipoints=7)
    conn = accounts.connect(db_path)
    code = accounts.create_code(conn, 60, llm_millipoints=2300)
    conn.close()
    client = TestClient(relay.create_relay_app(db_path))
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post("/v1/redeem", headers=headers, json={"code": code})
    assert first.status_code == 200
    assert first.json() == {"transcribe_seconds_remaining": 65}
    quota = client.get("/v1/quota", headers=headers).json()
    assert {key: quota[key] for key in ("label", "transcribe_seconds_remaining", "llm_points_remaining")} == {
        "label": "student-a", "transcribe_seconds_remaining": 65,
        "llm_points_remaining": 2.307,
    }
    assert quota["basic_seconds_remaining"] + quota["advanced_seconds_remaining"] == 65
    assert client.post("/v1/redeem", headers=headers, json={"code": code}).status_code == 400
    assert client.post(
        "/v1/redeem", headers=headers, json={"code": "UNKNOWN1"}
    ).status_code == 400
    assert _balances(db_path, account_id) == (65, 2307)


async def _overlapping_redeems(app, token, code):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay.test") as client:
        async def redeem():
            return await client.post(
                "/v1/redeem", headers={"Authorization": f"Bearer {token}"},
                json={"code": code},
            )
        return await asyncio.wait_for(
            asyncio.gather(redeem(), redeem()), timeout=30
        )


def test_concurrent_dual_grant_redemption_has_one_winner_and_one_credit(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "accounts.db"
    account_id, token = _provision(db_path)
    conn = accounts.connect(db_path)
    code = accounts.create_code(conn, 90, llm_millipoints=4100)
    conn.close()

    original_redeem = accounts.redeem_code
    barrier = threading.Barrier(2)
    entered_threads = []

    def overlapping_redeem(conn, candidate, candidate_account_id):
        entered_threads.append(threading.get_ident())
        barrier.wait(timeout=10)
        return original_redeem(conn, candidate, candidate_account_id)

    monkeypatch.setattr(accounts, "redeem_code", overlapping_redeem)
    responses = asyncio.run(
        _overlapping_redeems(relay.create_relay_app(db_path), token, code)
    )
    assert len(set(entered_threads)) == 2
    assert sorted(response.status_code for response in responses) == [200, 400]
    assert _balances(db_path, account_id) == (90, 4100)


def test_dual_grant_redemption_rolls_back_claim_and_both_balances(tmp_path):
    db_path = tmp_path / "accounts.db"
    account_id, _ = _provision(db_path, seconds=5, llm_millipoints=7)
    conn = accounts.connect(db_path)
    code = accounts.create_code(conn, 60, llm_millipoints=2300)
    conn.execute(
        "CREATE TRIGGER abort_dual_credit BEFORE UPDATE OF"
        " transcribe_seconds_remaining, llm_millipoints_remaining ON accounts"
        " BEGIN SELECT RAISE(ABORT, 'injected credit failure'); END"
    )
    conn.commit()
    try:
        accounts.redeem_code(conn, code, account_id)
    except sqlite3.IntegrityError as exc:
        assert "injected credit failure" in str(exc)
    else:
        raise AssertionError("injected account credit failure did not fire")
    assert _balances(db_path, account_id) == (5, 7)
    code_row = conn.execute(
        "SELECT redeemed_by, redeemed_at FROM codes WHERE code = ?", (code,)
    ).fetchone()
    assert tuple(code_row) == (None, None)
    conn.close()


def test_zero_llm_gate_then_redeem_allows_one_metered_retry(tmp_path):
    class OneCallLlm:
        def __init__(self):
            self.calls = 0

        def __call__(self, operation, system, user):
            self.calls += 1
            return Completion("done", 100, 20, 700, "stub-model")

    db_path = tmp_path / "accounts.db"
    account_id, token = _provision(db_path, llm_millipoints=0)
    vendor = OneCallLlm()
    client = TestClient(relay.create_relay_app(db_path, llm_fn=vendor))
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"operation": "grade", "system": "system", "user": "answer"}
    assert client.post("/v1/llm", headers=headers, json=payload).status_code == 402
    assert vendor.calls == 0

    conn = accounts.connect(db_path)
    code = accounts.create_code(conn, 0, llm_millipoints=700)
    conn.close()
    redeemed = client.post("/v1/redeem", headers=headers, json={"code": code})
    assert redeemed.status_code == 200
    assert redeemed.json() == {"transcribe_seconds_remaining": 0}
    assert client.get("/v1/quota", headers=headers).json()["llm_points_remaining"] == 0.7
    retried = client.post("/v1/llm", headers=headers, json=payload)
    assert retried.status_code == 200
    assert retried.json() == {"content": "done", "points_charged": 0.7}
    assert vendor.calls == 1
    assert _balances(db_path, account_id) == (0, 0)

    conn = accounts.connect(db_path)
    row = conn.execute(
        "SELECT operation, model, input_tokens, output_tokens, millipoints FROM llm_usage"
    ).fetchall()
    assert [tuple(item) for item in row] == [("grade", "stub-model", 100, 20, 700)]
    conn.close()


def test_activation_keeps_existing_initial_llm_allocation(tmp_path):
    db_path = tmp_path / "accounts.db"
    conn = accounts.connect(db_path)
    code = accounts.create_code(conn, 60, llm_millipoints=2300)
    conn.close()
    client = TestClient(relay.create_relay_app(db_path))
    activated = client.post("/v1/activate", json={"code": code})
    assert activated.status_code == 200
    token = activated.json()["platform_token"]
    quota = client.get(
        "/v1/quota", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert quota["llm_points_remaining"] == accounts.MONTHLY_LLM_MILLIPOINTS / 1000


def test_negative_llm_grant_is_rejected(tmp_path):
    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        try:
            accounts.create_code(conn, 60, llm_millipoints=-1)
        except ValueError as exc:
            assert str(exc) == "llm_millipoints must be non-negative"
        else:
            raise AssertionError("negative LLM grant was accepted")
        assert conn.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 0
    finally:
        conn.close()


def test_f2_create_code_command_persists_explicit_llm_grant(tmp_path):
    db_path = tmp_path / "f2.db"
    out_path = tmp_path / "code.txt"
    script = Path(__file__).parents[1] / "scripts" / "e2e_window_f2_relay.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "create-code",
            "--db",
            str(db_path),
            "--out",
            str(out_path),
            "--seconds",
            "90",
            "--llm-millipoints",
            "4100",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    code = out_path.read_text(encoding="utf-8").strip()
    conn = accounts.connect(db_path)
    try:
        row = conn.execute(
            "SELECT transcribe_seconds, llm_millipoints FROM codes WHERE code = ?",
            (code,),
        ).fetchone()
        assert tuple(row) == (90, 4100)
    finally:
        conn.close()
