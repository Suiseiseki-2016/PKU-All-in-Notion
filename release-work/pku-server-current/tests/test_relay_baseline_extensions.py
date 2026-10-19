"""Relocated coverage from the pre-existing baseline test files.

M1 scrutiny found mission-era tests added INSIDE the pre-existing
tests/test_accounts.py and tests/test_relay.py instead of additive
files (VAL-DEDUP-026 zero-edits condition). Those two files are now
byte-identical to the e465543 baseline. The displaced tests whose
behavior was NOT already pinned in the mission's additive files live
here, verbatim, with local fixture/helper copies so nothing depends on
the restored files:

- test_monthly_quota_refreshes_both_balances (accounts store unit):
  the 30-day rollover resets BOTH the transcription seconds and the
  LLM millipoints, pinned nowhere else among the additive files.
- test_transcribe_uses_streaming_vendor_without_reading_all_bytes
  (relay route leg): the streaming vendor receives the multipart
  filename and a spooled source it can read itself (no eager buffering
  by the route) — the filename passthrough is unique to this test.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from server import accounts, relay


def db(tmp_path):
    return accounts.connect(tmp_path / "accounts.db")


def make_client(tmp_path, asr_fn=None, streaming_asr_fn=None, quota_seconds=600):
    conn = accounts.connect(tmp_path / "accounts.db")
    account_id = accounts.create_account(conn, "同学A")
    if quota_seconds:
        conn.execute(
            "UPDATE accounts SET transcribe_seconds_remaining = ? WHERE id = ?",
            (quota_seconds, account_id),
        )
        conn.commit()
    token = accounts.issue_session(conn, account_id)
    conn.close()
    app = relay.create_relay_app(
        tmp_path / "accounts.db", asr_fn=asr_fn, streaming_asr_fn=streaming_asr_fn
    )
    return TestClient(app), token


def upload(client, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(
        "/v1/transcribe",
        headers=headers,
        files={"audio": ("a.m4a", b"fake-audio", "audio/mp4")},
    )


def test_monthly_quota_refreshes_both_balances(tmp_path, monkeypatch):
    conn = db(tmp_path)
    account_id = accounts.create_account(conn, "同学A")
    conn.execute(
        "UPDATE accounts SET transcribe_seconds_remaining = 1,"
        " llm_millipoints_remaining = 2, period_started_at = 1 WHERE id = ?",
        (account_id,),
    )
    conn.commit()
    monkeypatch.setattr(accounts.time, "time", lambda: accounts.MONTH_SECONDS + 2)
    assert accounts.remaining_seconds(conn, account_id) == accounts.MONTHLY_TRANSCRIBE_SECONDS
    assert accounts.remaining_llm_millipoints(conn, account_id) == 100_000
    conn.close()


def test_transcribe_uses_streaming_vendor_without_reading_all_bytes(tmp_path):
    def streaming_vendor(source, filename):
        assert filename == "a.m4a"
        assert source.read() == b"fake-audio"
        return {"duration_seconds": 60, "segments": [{"start": 0, "text": "流式"}]}

    client, token = make_client(tmp_path, streaming_asr_fn=streaming_vendor)
    assert upload(client, token).json()["segments"][0]["text"] == "流式"
