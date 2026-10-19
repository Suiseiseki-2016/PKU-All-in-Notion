"""Transcript reuse store tests (temp SQLite, no network).

Pins the durable exact-fingerprint dedup store: approved schema and
privacy, migration off a pre-feature DB, upsert/expiry/prune lifecycle,
cross-connection durability, and exact-sha256-only lookup.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from io import BytesIO

import pytest

from server import accounts, asr, transcript_store

APPROVED_COLUMNS = {
    "fingerprint_sha256",
    "transcript_result",
    "created_at",
    "updated_at",
    "model",
    "model_version",
    "expires_at",
}

FORBIDDEN_COLUMN_WORDS = (
    "account",
    "user",
    "owner",
    "label",
    "platform",
    "pku",
    "credential",
    "cookie",
    "token",
    "notion",
    "filename",
    "path",
    "audio",
    "blob",
)

DEFAULT_TTL = 30 * 24 * 3600


def db(tmp_path):
    return accounts.connect(tmp_path / "accounts.db")


def payload(text="第一讲", duration=120):
    return {
        "language": "zh",
        "duration_seconds": duration,
        "segments": [{"start": 0.0, "end": 1.5, "text": text}],
    }


def reuse_row(conn):
    return conn.execute("SELECT * FROM transcript_reuse").fetchone()


def patch_clock(monkeypatch, start):
    clock = {"now": float(start)}
    monkeypatch.setattr(transcript_store.time, "time", lambda: clock["now"])
    return clock


# --- schema exactness and privacy (VAL-DEDUP-001) ---


def test_dedup_table_schema_is_exact_and_private(tmp_path):
    conn = db(tmp_path)
    columns = {
        row["name"]: row for row in conn.execute("PRAGMA table_info(transcript_reuse)")
    }
    assert set(columns) == APPROVED_COLUMNS
    fingerprint = columns["fingerprint_sha256"]
    assert fingerprint["type"] == "TEXT"
    assert fingerprint["notnull"] == 1
    assert fingerprint["pk"] == 1  # sole primary key
    assert columns["transcript_result"]["notnull"] == 1
    for name, column in columns.items():
        assert column["pk"] == 0 or name == "fingerprint_sha256"
        lowered = name.lower()
        for word in FORBIDDEN_COLUMN_WORDS:
            assert word not in lowered, f"forbidden column name: {name}"
    assert list(conn.execute("PRAGMA foreign_key_list(transcript_reuse)")) == []
    schema_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='transcript_reuse'"
    ).fetchone()["sql"]
    assert "PRIMARY KEY" in schema_sql
    assert "references" not in schema_sql.lower()
    conn.close()


# --- migration off a pre-feature DB (VAL-DEDUP-002) ---


def test_pre_feature_db_gains_dedup_table_idempotently(tmp_path):
    path = tmp_path / "accounts.db"
    raw = sqlite3.connect(path)
    raw.executescript(accounts.SCHEMA)  # exactly the pre-feature schema
    raw.execute("INSERT INTO accounts (label, created_at) VALUES ('同学A', 100.0)")
    raw.execute(
        "INSERT INTO sessions (token, account_id, issued_at, expires_at)"
        " VALUES ('pre-existing-session-token', 1, 100.0, 4102444800.0)"
    )
    raw.execute(
        "INSERT INTO usage (account_id, kind, seconds, recorded_at)"
        " VALUES (1, 'transcribe', 60, 101.0)"
    )
    raw.commit()
    tables_before = {
        row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    raw.close()
    assert "transcript_reuse" not in tables_before

    conn = accounts.connect(path)  # first new-code connect: migration runs
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "transcript_reuse" in tables
    migrated = {
        row["name"] for row in conn.execute("PRAGMA table_info(transcript_reuse)")
    }
    assert migrated == APPROVED_COLUMNS
    account = accounts.validate_session(conn, "pre-existing-session-token")
    assert account is not None and account["label"] == "同学A"  # rows intact
    assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 1
    conn.close()

    conn = accounts.connect(path)  # second connect: no-op, no duplicates
    reuse_tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master"
        " WHERE type='table' AND name='transcript_reuse'"
    ).fetchone()[0]
    assert reuse_tables == 1
    assert accounts.validate_session(conn, "pre-existing-session-token") is not None
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 1
    conn.close()


# --- row privacy: no account/token/filename/OSS key/audio (VAL-DEDUP-003) ---


def test_stored_row_contains_no_account_token_filename_or_audio(tmp_path):
    conn = db(tmp_path)
    a_id = accounts.create_account(conn, "同学A")
    b_id = accounts.create_account(conn, "同学B")
    token_a = accounts.issue_session(conn, a_id)
    token_b = accounts.issue_session(conn, b_id)
    accounts.record_transcription(conn, a_id, 120)
    audio = b"fake-audio-xyz"  # uploaded in the scenario as "a.m4a"
    fingerprint = transcript_store.fingerprint_of(audio)
    transcript_store.store_transcript(conn, fingerprint, payload())

    row = reuse_row(conn)
    stored = json.loads(row["transcript_result"])
    assert set(stored) <= set(payload().keys())  # payload keys only

    dump = [str(key) + "=" + str(row[key]) for key in row.keys()]
    dump.append(str(stored))  # decoded view (no \u escapes)
    dump.append(json.dumps(stored, ensure_ascii=False))
    dump.extend(part.encode("utf-8", "surrogatepass").hex() for part in list(dump))
    dump_text = "\n".join(dump)
    for forbidden in (
        "同学A",
        "同学B",
        token_a,
        token_b,
        "a.m4a",
        "pku-temp/",
        "fake-audio-xyz",  # audio bytes as text
        audio.hex(),  # audio bytes as hex
    ):
        assert forbidden not in dump_text
    conn.close()


# --- fingerprint correctness on both paths (VAL-DEDUP-004) ---


def test_bytes_path_fingerprint_matches_sha256_of_exact_bytes():
    audio = b"0123456789abcdef" * 1000
    assert transcript_store.fingerprint_of(audio) == hashlib.sha256(audio).hexdigest()


def test_streaming_spool_fingerprint_matches_sha256_of_exact_bytes():
    class TrickleSource:
        """Short-read source: proves chunk boundaries never affect the digest."""

        def __init__(self, data):
            self._data = data
            self._pos = 0

        def read(self, size=-1):
            if size is None or size < 0:
                size = len(self._data) - self._pos
            chunk = self._data[self._pos : self._pos + min(size, 7)]
            self._pos += len(chunk)
            return bytes(chunk)

    audio = b"0123456789abcdef" * 200_000  # ~3.2MB, spans several chunks
    expected = hashlib.sha256(audio).hexdigest()
    spooled, digest = transcript_store.spool_and_hash(BytesIO(audio))
    assert digest == expected
    assert spooled.read() == audio  # the vendor sees the exact same bytes
    spooled.close()

    trickled, trickle_digest = transcript_store.spool_and_hash(TrickleSource(audio))
    assert trickle_digest == expected  # only the bytes matter, never the reads
    assert trickled.read() == audio
    trickled.close()


def test_stored_fingerprint_equals_sha256_on_both_paths(tmp_path):
    conn = db(tmp_path)
    audio = b"exact-bytes-for-fingerprint"
    expected = hashlib.sha256(audio).hexdigest()

    fp_bytes = transcript_store.fingerprint_of(audio)
    transcript_store.store_transcript(conn, fp_bytes, payload("bytes-path"))
    assert reuse_row(conn)["fingerprint_sha256"] == expected

    spooled, fp_stream = transcript_store.spool_and_hash(BytesIO(audio))
    spooled.close()
    assert fp_stream == fp_bytes == expected  # multipart metadata never enters
    conn.close()


# --- upsert semantics: one row per fingerprint (VAL-DEDUP-005) ---


def test_same_fingerprint_stored_twice_leaves_one_row_second_write_wins(
    tmp_path, monkeypatch
):
    conn = db(tmp_path)
    clock = patch_clock(monkeypatch, 1000.0)
    fp = transcript_store.fingerprint_of(b"audio-one")
    transcript_store.store_transcript(conn, fp, payload("first"))
    clock["now"] += 10.0
    transcript_store.store_transcript(conn, fp, payload("second"))

    rows = conn.execute("SELECT * FROM transcript_reuse").fetchall()
    assert len(rows) == 1  # never two rows for one fingerprint
    row = rows[0]
    assert json.loads(row["transcript_result"]) == payload("second")
    assert row["created_at"] == 1010.0  # timestamps from the second write
    assert row["updated_at"] == 1010.0
    assert row["expires_at"] == 1010.0 + DEFAULT_TTL  # expiry refreshed with it

    fp_other = transcript_store.fingerprint_of(b"audio-two")
    transcript_store.store_transcript(conn, fp_other, payload("other"))
    assert conn.execute("SELECT COUNT(*) FROM transcript_reuse").fetchone()[0] == 2
    conn.close()


# --- lifecycle metadata across a hit (VAL-DEDUP-006) ---


def test_lifecycle_metadata_and_hit_touches_updated_at_only(tmp_path, monkeypatch):
    import dashscope

    conn = db(tmp_path)
    clock = patch_clock(monkeypatch, 1000.0)
    fp = transcript_store.fingerprint_of(b"audio-lifecycle")
    transcript_store.store_transcript(conn, fp, payload())
    row = reuse_row(conn)
    assert row["model"] == asr.MODEL == "qwen-audio-3.0-asr-flash-filetrans"
    expected_version = getattr(dashscope, "__version__", "") or (
        transcript_store.FALLBACK_MODEL_VERSION
    )
    assert row["model_version"] == expected_version
    assert row["model_version"]  # non-empty
    created_at = row["created_at"]
    assert row["updated_at"] == created_at  # born equal
    assert row["updated_at"] >= row["created_at"]

    clock["now"] += 50.0
    hit = transcript_store.lookup(conn, fp)
    assert hit is not None
    assert json.loads(hit["transcript_result"]) == payload()
    row = reuse_row(conn)
    assert row["created_at"] == created_at  # immutable across the hit
    assert row["updated_at"] == created_at + 50.0  # refreshed by the hit
    assert row["updated_at"] > row["created_at"]
    assert row["updated_at"] >= row["created_at"]
    conn.close()


# --- bounded retention; expired row is a miss that replaces (VAL-DEDUP-007) ---


def test_retention_bounded_and_expired_row_is_a_miss_that_replaces(
    tmp_path, monkeypatch
):
    conn = db(tmp_path)
    clock = patch_clock(monkeypatch, 2000.0)
    fp = transcript_store.fingerprint_of(b"audio-retention")
    transcript_store.store_transcript(conn, fp, payload("original"))
    row = reuse_row(conn)
    assert row["expires_at"] == row["created_at"] + DEFAULT_TTL
    expires_at = row["expires_at"]

    clock["now"] += 60.0
    assert transcript_store.lookup(conn, fp) is not None
    clock["now"] += 3600.0
    assert transcript_store.lookup(conn, fp) is not None
    assert reuse_row(conn)["expires_at"] == expires_at  # hits never extend it

    clock["now"] = expires_at
    assert transcript_store.lookup(conn, fp) is None  # expires_at == now is a miss

    clock["now"] = expires_at + 10.0
    transcript_store.store_transcript(conn, fp, payload("retranscribed"))
    rows = conn.execute("SELECT * FROM transcript_reuse").fetchall()
    assert len(rows) == 1  # single replaced row
    row = rows[0]
    assert json.loads(row["transcript_result"]) == payload("retranscribed")
    assert row["created_at"] == expires_at + 10.0  # refreshed together
    assert row["updated_at"] == row["created_at"]
    assert row["expires_at"] == row["created_at"] + DEFAULT_TTL
    assert row["model"] == asr.MODEL and row["model_version"]  # provenance survives
    clock["now"] += 5.0
    assert transcript_store.lookup(conn, fp) is not None  # serves hits again
    conn.close()


def test_ttl_is_env_overridable(tmp_path, monkeypatch):
    conn = db(tmp_path)
    clock = patch_clock(monkeypatch, 3000.0)
    monkeypatch.setenv("TRANSCRIPT_REUSE_TTL_SECONDS", "3600")
    fp = transcript_store.fingerprint_of(b"audio-ttl-override")
    transcript_store.store_transcript(conn, fp, payload())
    row = reuse_row(conn)
    assert row["expires_at"] == row["created_at"] + 3600

    monkeypatch.setenv("TRANSCRIPT_REUSE_TTL_SECONDS", "not-a-number")
    fp_other = transcript_store.fingerprint_of(b"audio-ttl-invalid")
    transcript_store.store_transcript(conn, fp_other, payload())
    row = conn.execute(
        "SELECT * FROM transcript_reuse WHERE fingerprint_sha256 = ?", (fp_other,)
    ).fetchone()
    assert row["expires_at"] == row["created_at"] + DEFAULT_TTL
    conn.close()


# --- pruning removes expired rows only (VAL-DEDUP-008) ---


def test_prune_removes_only_expired_rows_and_live_rows_still_hit(tmp_path, monkeypatch):
    conn = db(tmp_path)
    clock = patch_clock(monkeypatch, 5000.0)
    monkeypatch.setenv("TRANSCRIPT_REUSE_TTL_SECONDS", "100")
    fp_expired = transcript_store.fingerprint_of(b"audio-old")
    transcript_store.store_transcript(conn, fp_expired, payload("old"))  # expires 5100
    monkeypatch.setenv("TRANSCRIPT_REUSE_TTL_SECONDS", "3600")
    fp_live = transcript_store.fingerprint_of(b"audio-new")
    transcript_store.store_transcript(conn, fp_live, payload("new"))  # expires 8600

    clock["now"] = 5150.0
    assert transcript_store.prune_expired(conn) == 1
    remaining = conn.execute(
        "SELECT fingerprint_sha256 FROM transcript_reuse"
    ).fetchall()
    assert [row["fingerprint_sha256"] for row in remaining] == [fp_live]

    clock["now"] += 10.0
    hit = transcript_store.lookup(conn, fp_live)
    assert hit is not None  # live rows still serve hits after the prune
    assert json.loads(hit["transcript_result"]) == payload("new")
    conn.close()


# --- durability: a second connection on the same DB file (VAL-DEDUP-009) ---


def test_second_connection_on_same_db_serves_hit_with_identical_payload(
    tmp_path, monkeypatch
):
    path = tmp_path / "accounts.db"
    conn = accounts.connect(path)
    clock = patch_clock(monkeypatch, 1000.0)
    fp = transcript_store.fingerprint_of(b"audio-durable")
    transcript_store.store_transcript(conn, fp, payload("durable"))
    created_at = reuse_row(conn)["created_at"]
    conn.close()

    conn = accounts.connect(path)  # a second app instance on the same DB file
    hit = transcript_store.lookup(conn, fp)
    assert hit is not None
    assert json.loads(hit["transcript_result"]) == payload("durable")
    assert hit["created_at"] == created_at  # not an in-memory cache
    conn.close()


# --- exact-sha256-only lookup; no fuzzy surface (VAL-DEDUP-016) ---


def test_lookup_accepts_only_the_exact_sha256(tmp_path):
    conn = db(tmp_path)
    fp = transcript_store.fingerprint_of(b"audio-exactness")
    transcript_store.store_transcript(conn, fp, payload())
    assert transcript_store.lookup(conn, fp) is not None  # the one and only hit

    substitution = fp[:63] + ("0" if fp[63] != "0" else "1")
    near_misses = [
        fp.upper(),  # uppercase hex
        fp[:63],  # 63-char prefix
        substitution,  # one-char substitution
        fp + "0",  # 65 chars
        "z" * 64,  # 64 chars, not hex
        hashlib.sha256(b"different-bytes").hexdigest(),  # different digest
        "",  # empty string
        "课堂录像笔记 第一讲.m4a",  # title-like string
        "a.m4a",  # filename-like string
        None,
        12345,
    ]
    for candidate in near_misses:
        assert transcript_store.lookup(conn, candidate) is None
    conn.close()


def test_store_api_has_no_fuzzy_lookup_parameters():
    assert set(inspect.signature(transcript_store.lookup).parameters) == {
        "conn",
        "fingerprint",
    }
    assert set(inspect.signature(transcript_store.store_transcript).parameters) == {
        "conn",
        "fingerprint",
        "transcript_result",
    }


def test_store_rejects_non_fingerprint_keys(tmp_path):
    conn = db(tmp_path)
    for bad_key in ("not-a-fingerprint", "", "A" * 64):
        with pytest.raises(ValueError):
            transcript_store.store_transcript(conn, bad_key, payload())
    assert conn.execute("SELECT COUNT(*) FROM transcript_reuse").fetchone()[0] == 0
    conn.close()
