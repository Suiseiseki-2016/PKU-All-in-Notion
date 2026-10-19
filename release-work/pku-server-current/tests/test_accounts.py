"""Platform account store tests (temp SQLite, no network)."""

from __future__ import annotations

from server import accounts


def db(tmp_path):
    return accounts.connect(tmp_path / "accounts.db")


def test_session_roundtrip(tmp_path):
    conn = db(tmp_path)
    account_id = accounts.create_account(conn, "同学A")
    token = accounts.issue_session(conn, account_id)
    row = accounts.validate_session(conn, token)
    assert row is not None and row["label"] == "同学A"
    assert accounts.validate_session(conn, "nonsense") is None
    conn.close()


def test_expired_session_is_rejected(tmp_path):
    conn = db(tmp_path)
    account_id = accounts.create_account(conn, "同学A")
    token = accounts.issue_session(conn, account_id, ttl=-1)
    assert accounts.validate_session(conn, token) is None
    conn.close()


def test_disabled_account_is_rejected(tmp_path):
    conn = db(tmp_path)
    account_id = accounts.create_account(conn, "同学A")
    token = accounts.issue_session(conn, account_id)
    conn.execute("UPDATE accounts SET status = 'disabled' WHERE id = ?", (account_id,))
    conn.commit()
    assert accounts.validate_session(conn, token) is None
    conn.close()


def test_code_redeem_adds_quota_once(tmp_path):
    conn = db(tmp_path)
    account_id = accounts.create_account(conn, "同学A")
    code = accounts.create_code(conn, 600)
    assert accounts.redeem_code(conn, code, account_id) is True
    assert accounts.remaining_seconds(conn, account_id) == 600
    assert accounts.redeem_code(conn, code, account_id) is False  # single-use
    conn.close()


def test_transcription_metering(tmp_path):
    conn = db(tmp_path)
    account_id = accounts.create_account(conn, "同学A")
    accounts.redeem_code(conn, accounts.create_code(conn, 600), account_id)
    accounts.record_transcription(conn, account_id, 90)
    assert accounts.remaining_seconds(conn, account_id) == 510
    rows = conn.execute("SELECT kind, seconds FROM usage").fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "transcribe"
    assert rows[0]["seconds"] == 90
    conn.close()
