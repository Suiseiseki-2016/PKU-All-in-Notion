"""Route-level durability of the reuse store across relay restarts (VAL-DEDUP-009).

The store-level second-connection test (tests/test_transcript_store.py)
proves the lookup layer survives a new connection; this file pins the
SAME guarantee at the /v1/transcribe route: a SECOND relay app instance
on the same DB file (a fresh ``relay.create_relay_app`` call, the
durable equivalent of a process restart or redeploy) serves an
exact-fingerprint hit with ``reused=true``, a key-for-key identical
transcript payload, zero additional upstream calls on the SHARED
counting vendor, an unchanged row ``created_at``, and no charge on the
second caller. Requests here are sequential; genuinely overlapping
requests belong to the concurrency feature.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from server import accounts, relay

MISS_SECONDS = 120  # the counting vendor's single ceil'd duration


def make_accounts(tmp_path):
    """One fresh DB with two provisioned accounts; returns path plus tokens."""
    path = tmp_path / "accounts.db"
    conn = accounts.connect(path)
    tokens = {}
    for label in ("同学A", "同学B"):
        account_id = accounts.create_account(conn, label)
        conn.execute(
            "UPDATE accounts SET transcribe_seconds_remaining = 600 WHERE id = ?",
            (account_id,),
        )
        conn.commit()
        tokens[label] = accounts.issue_session(conn, account_id)
    conn.close()
    return path, tokens["同学A"], tokens["同学B"]


class CountingVendor:
    """Streaming vendor fake keyed on the exact bytes, with a call counter."""

    def __init__(self):
        self.calls = 0

    def __call__(self, source, filename):
        self.calls += 1
        data = source.read()
        return {
            "language": "zh",
            "duration_seconds": MISS_SECONDS,
            "segments": [{"start": 0.0, "end": 1.5, "text": f"transcript:{data!r}"}],
        }


def strip_reuse_fields(body):
    return {k: v for k, v in body.items() if k not in ("reused", "seconds_charged")}


def upload(client, token, audio):
    return client.post(
        "/v1/transcribe",
        headers={"Authorization": f"Bearer {token}"},
        files={"audio": ("lecture.m4a", audio, "audio/mp4")},
    )


def quota_of(client, token):
    return client.get("/v1/quota", headers={"Authorization": f"Bearer {token}"}).json()


def reuse_row(db_path):
    conn = accounts.connect(db_path)
    found = conn.execute("SELECT * FROM transcript_reuse").fetchone()
    conn.close()
    return found


def usage_rows(db_path):
    conn = accounts.connect(db_path)
    found = conn.execute("SELECT * FROM usage ORDER BY id").fetchall()
    conn.close()
    return found


def account_id_of(db_path, label):
    conn = accounts.connect(db_path)
    found = conn.execute("SELECT id FROM accounts WHERE label = ?", (label,)).fetchone()
    conn.close()
    return found["id"]


def test_second_app_instance_on_same_db_serves_hit_zero_upstream_calls(tmp_path):
    audio = b"restart-durable-exact-bytes"
    db_path, token_a, token_b = make_accounts(tmp_path)
    vendor = CountingVendor()  # ONE counting vendor shared by both app instances

    # Instance 1: a fresh relay on a fresh DB. The first transcribe is a
    # miss: the vendor runs once, the caller is charged, the row is stored.
    client_one = TestClient(relay.create_relay_app(db_path, streaming_asr_fn=vendor))
    miss = upload(client_one, token_a, audio)
    assert miss.status_code == 200
    miss_body = miss.json()
    assert miss_body["reused"] is False
    assert miss_body["seconds_charged"] == MISS_SECONDS
    assert vendor.calls == 1
    assert "duration_seconds" not in miss_body
    created_at_before = reuse_row(db_path)["created_at"]
    assert (
        quota_of(client_one, token_a)["transcribe_seconds_remaining"]
        == 600 - MISS_SECONDS
    )

    # Instance 2: a SECOND app on the SAME DB file (a relay restart),
    # sharing the SAME counting vendor object. The identical bytes must be
    # an exact-fingerprint hit, served straight from the durable row.
    client_two = TestClient(relay.create_relay_app(db_path, streaming_asr_fn=vendor))
    hit = upload(client_two, token_b, audio)
    assert hit.status_code == 200
    hit_body = hit.json()
    assert hit_body["reused"] is True
    assert hit_body["seconds_charged"] == 0
    assert vendor.calls == 1  # zero additional upstream calls after restart
    assert "duration_seconds" not in hit_body

    # Key-for-key payload equality (established VAL-DEDUP-010/022 pattern):
    # only the explicit reuse/settlement fields differ, nothing else.
    assert strip_reuse_fields(hit_body) == strip_reuse_fields(miss_body)
    assert hit_body["segments"] == miss_body["segments"]
    differing = {
        key
        for key in set(miss_body) | set(hit_body)
        if miss_body.get(key) != hit_body.get(key)
    }
    assert differing == {"reused", "seconds_charged"}

    # The row is durable, not an in-memory cache: created_at is untouched
    # by the restart and its hit (only updated_at may move).
    assert reuse_row(db_path)["created_at"] == created_at_before

    # The second caller was never charged; the miss's charge stands once.
    assert (
        quota_of(client_two, token_b)["transcribe_seconds_remaining"] == 600
    )
    assert (
        quota_of(client_two, token_a)["transcribe_seconds_remaining"]
        == 600 - MISS_SECONDS
    )
    rows = usage_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "transcribe"
    assert rows[0]["seconds"] == MISS_SECONDS
    assert rows[0]["account_id"] == account_id_of(db_path, "同学A")
