"""Account-bound, short-lived direct audio uploads to private OSS.

Only audio segments are accepted. A private server-side copy freezes the bytes
before hashing, deduplication and ASR, so a still-valid PUT URL cannot poison
another account's exact-fingerprint transcript cache.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from . import accounts, transcript_store, account_evidence
from .asr import AsrError

UPLOAD_TTL = 15 * 60
SCHEMA = """
CREATE TABLE IF NOT EXISTS direct_upload_grants (
    id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    upload_key TEXT NOT NULL,
    private_key TEXT NOT NULL,
    expected_bytes INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'issued',
    fingerprint TEXT,
    seconds_charged INTEGER,
    reused INTEGER
)
"""


class DirectUploadError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(detail)


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(SCHEMA)
    columns = {row[1] for row in db.execute('PRAGMA table_info(direct_upload_grants)')}
    if 'source_id' not in columns:
        db.execute('ALTER TABLE direct_upload_grants ADD COLUMN source_id TEXT')
    if 'part_index' not in columns:
        db.execute('ALTER TABLE direct_upload_grants ADD COLUMN part_index INTEGER NOT NULL DEFAULT 0')
    if 'transcribe_mode' not in columns:
        db.execute("ALTER TABLE direct_upload_grants ADD COLUMN transcribe_mode TEXT NOT NULL DEFAULT 'basic'")


def cleanup_expired(db: sqlite3.Connection, store) -> None:
    now = time.time()
    rows = db.execute(
        "SELECT id, account_id, source_id, upload_key, private_key, state, fingerprint, seconds_charged, reused, expires_at FROM direct_upload_grants "
        "WHERE (expires_at < ? AND state != 'processing') "
        "OR (expires_at < ? AND state = 'processing')",
        (now, now - 3600),
    ).fetchall()
    for row in rows:
        for key in (row["upload_key"], row["private_key"]):
            try:
                with account_evidence.oss_context(db.execute("PRAGMA database_list").fetchone()[2], row["account_id"], "asr:" + row["id"], row["source_id"]):
                    store.delete(key)
                    if account_evidence.head_object(store, key)["exists"] is not False:
                        raise RuntimeError("expired OSS object deletion unverified")
            except Exception:
                # A later request or the OSS lifecycle rule can retry cleanup.
                break
        else:
            # OSS deletion has already been freshly verified. Commit each
            # metadata receipt/removal pair before the next separate OSS
            # evidence connection; otherwise cleanup locks its own writer.
            with db:
                account_evidence.retain_direct_upload_job(db, row)
                db.execute("DELETE FROM direct_upload_grants WHERE id = ?", (row["id"],))
    db.commit()


def issue(db: sqlite3.Connection, account_id: int, store, expected_bytes: int, source_id=None, part_index: int = 0, transcribe_mode: str = "basic") -> dict:
    cleanup_expired(db, store)
    identifier = uuid.uuid4().hex
    day = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    upload_key = f"pku-temp/direct/{day}/{identifier}.m4a"
    private_key = f"pku-temp/internal/{day}/{uuid.uuid4().hex}.m4a"
    expires_at = time.time() + UPLOAD_TTL
    upload_url = store.bucket.sign_url("PUT", upload_key, UPLOAD_TTL, slash_safe=True,
                                       headers={"Content-Length": str(expected_bytes), "Content-Type": "audio/mp4"})
    db.execute(
        "INSERT INTO direct_upload_grants(id, account_id, upload_key, private_key, expected_bytes, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (identifier, account_id, upload_key, private_key, expected_bytes, expires_at),
    )
    db.commit()
    mode_norm = "advanced" if transcribe_mode == "advanced" else "basic"
    db.execute(
        "UPDATE direct_upload_grants SET source_id=?, part_index=?, transcribe_mode=? WHERE id=?",
        (source_id, int(part_index), mode_norm, identifier),
    )
    db.commit()
    with account_evidence.oss_context(db.execute("PRAGMA database_list").fetchone()[2], account_id, "asr:" + identifier, source_id):
        for key in (upload_key, private_key):
            account_evidence.record_object(key, "grant-issued")
    return {"upload_id": identifier, "upload_url": upload_url, "expires_at": expires_at}


def complete(state, account_id: int, upload_id: str, max_bytes: int, max_seconds: int) -> dict:
    db = accounts.connect(state.db_path)
    store = state.streaming_asr_fn.store
    source = db.execute('SELECT source_id, part_index, transcribe_mode FROM direct_upload_grants WHERE id=? AND account_id=?', (upload_id, account_id)).fetchone()
    grant_source_id = source['source_id'] if source else None
    grant_part_index = int(source['part_index'] or 0) if source else 0
    grant_mode = (source['transcribe_mode'] or 'basic') if source else 'basic'
    context = account_evidence.oss_context(state.db_path, account_id, 'asr:' + upload_id, grant_source_id)
    context.__enter__()
    try:
        changed = db.execute(
            "UPDATE direct_upload_grants SET state = 'processing' "
            "WHERE id = ? AND account_id = ? AND state = 'issued' AND expires_at > ?",
            (upload_id, account_id, time.time()),
        ).rowcount
        db.commit()
        if not changed:
            previous = db.execute(
                "SELECT fingerprint, seconds_charged, reused FROM direct_upload_grants "
                "WHERE id = ? AND account_id = ? AND state = 'done' AND expires_at > ?",
                (upload_id, account_id, time.time()),
            ).fetchone()
            if previous is not None and previous["fingerprint"]:
                row = transcript_store.lookup(db, previous["fingerprint"])
                if row is not None:
                    return {"payload": json.loads(row["transcript_result"]),
                            "reused": bool(previous["reused"]),
                            "seconds_charged": previous["seconds_charged"]}
            raise DirectUploadError(409, "upload authorization expired or already used")
        grant = db.execute(
            "SELECT upload_key, private_key, expected_bytes FROM direct_upload_grants WHERE id = ?",
            (upload_id,),
        ).fetchone()
        upload_key, private_key = grant["upload_key"], grant["private_key"]
        try:
            if accounts.remaining_seconds(db, account_id) <= 0:
                raise DirectUploadError(402, "transcription quota exhausted")
            # OSS-to-OSS copy is private and fast. The client can no longer
            # mutate the bytes that become a shared cache fingerprint.
            copied = store.bucket.copy_object(store.bucket.bucket_name, upload_key, private_key)
            if copied.status != 200:
                raise DirectUploadError(502, "OSS could not finalize audio")
            observed = account_evidence.head_object(store, upload_key)
            account_evidence.record_object(upload_key, 'created', exists=observed['exists'], size=observed['bytes'])
            store.delete(upload_key)
            head = store.bucket.head_object(private_key)
            account_evidence.record_object(private_key, 'created', exists=True, size=int(head.content_length))
            length = int(head.content_length)
            if length <= 0 or length > max_bytes or length != grant["expected_bytes"]:
                raise DirectUploadError(413, "audio file is empty or too large")
            digest = hashlib.sha256()
            received = 0
            response = store.bucket.get_object(private_key)
            try:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > max_bytes:
                        raise DirectUploadError(413, "audio file too large")
                    digest.update(chunk)
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
            if received != length:
                raise DirectUploadError(502, "OSS audio read incomplete")
            fingerprint = digest.hexdigest()
            with state.in_flight.hold("lecture:" + grant_source_id + ":" + str(grant_part_index) if grant_source_id else fingerprint):
                row = transcript_store.lookup(db, fingerprint)
                if row is None and grant_source_id:
                    row = transcript_store.lookup_lecture_part(db, grant_source_id, grant_part_index)
                if row is not None:
                    payload = json.loads(row["transcript_result"])
                    # Completion retries resolve by the new upload fingerprint.
                    if row["fingerprint_sha256"] != fingerprint:
                        transcript_store.store_transcript(db, fingerprint, payload)
                    if grant_source_id and transcript_store.lookup_lecture_part(db, grant_source_id, grant_part_index) is None:
                        transcript_store.store_lecture_part(db, grant_source_id, grant_part_index, fingerprint, payload)
                    from .relay import _record_cache_metric
                    _record_cache_metric(db, account_id, reused=True, payload=payload)
                    db.execute("UPDATE direct_upload_grants SET fingerprint=?, seconds_charged=0, reused=1 WHERE id=?", (fingerprint, upload_id))
                    db.commit()
                    return {"payload": payload, "reused": True, "seconds_charged": 0}
                try:
                    payload = state.streaming_asr_fn.transcribe_uploaded(private_key)
                except AsrError as exc:
                    raise DirectUploadError(502, "transcription failed upstream") from exc
                seconds = math.ceil(payload["duration_seconds"])
                if seconds > max_seconds:
                    raise DirectUploadError(413, "audio exceeds duration cap")
                accounts.record_transcription(db, account_id, seconds, operation_id='asr:' + upload_id, mode=grant_mode)
                transcript_store.store_transcript(db, fingerprint, payload)
                if grant_source_id:
                    transcript_store.store_lecture_part(db, grant_source_id, grant_part_index, fingerprint, payload)
                from .relay import _record_cache_metric
                _record_cache_metric(db, account_id, reused=False, payload=payload)
                db.execute("UPDATE direct_upload_grants SET fingerprint=?, seconds_charged=?, reused=0 WHERE id=?", (fingerprint, seconds, upload_id))
                db.commit()
                return {"payload": payload, "reused": False, "seconds_charged": seconds}
        finally:
            for key in (upload_key, private_key):
                try:
                    store.delete(key)
                except Exception:
                    pass
            db.execute("UPDATE direct_upload_grants SET state = 'done' WHERE id = ?", (upload_id,))
            db.commit()
    finally:
        context.__exit__(None, None, None)
        db.close()


def cleanup_database(db_path, store) -> None:
    db = accounts.connect(db_path)
    try:
        cleanup_expired(db, store)
    finally:
        db.close()
