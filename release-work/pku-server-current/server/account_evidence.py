"""Authenticated cumulative billing and account-scoped operation/OSS evidence.

GETs open SQLite in read-only mode and hold one snapshot. Usage rows, not
remaining quota, define cumulative charges. Missing instrumentation fails
closed. This file is a deployment candidate; it does not deploy itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3
import time
import threading
from urllib.parse import quote

from fastapi import Header, HTTPException
from .asr_observation_archive import restore_observed_asr_jobs

_OSS_CONTEXT = ContextVar("oss_evidence_context", default=None)
MAX_RECORDS = 10000
HEAD_WORKERS = 8
HEAD_TIMEOUT_SECONDS = 12
_HEAD_POOL = ThreadPoolExecutor(max_workers=HEAD_WORKERS, thread_name_prefix='oss-evidence-head')
_HEAD_SLOTS = threading.BoundedSemaphore(2 * HEAD_WORKERS)
SCHEMA = """
CREATE TABLE IF NOT EXISTS oss_object_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
 operation_id TEXT NOT NULL, object_key TEXT NOT NULL, event TEXT NOT NULL,
 observed_at REAL NOT NULL, observed_exists INTEGER, bytes INTEGER,
 source_id TEXT
);
CREATE INDEX IF NOT EXISTS oss_events_account ON oss_object_events(account_id, id);
CREATE TABLE IF NOT EXISTS asr_job_receipts (
 id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, state TEXT NOT NULL,
 fingerprint TEXT, seconds_charged INTEGER, reused INTEGER, source_id TEXT,
 expires_at REAL NOT NULL, retained_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS asr_receipts_account ON asr_job_receipts(account_id, id);
CREATE TABLE IF NOT EXISTS asr_observed_jobs (
 id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, state TEXT NOT NULL,
 fingerprint TEXT NOT NULL, seconds_charged INTEGER NOT NULL, reused INTEGER NOT NULL,
 original_body_sha256 TEXT NOT NULL, original_body_bytes INTEGER NOT NULL,
 observed_at TEXT NOT NULL, restored_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS asr_observations_account ON asr_observed_jobs(account_id, id);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    for table, name, definition in (("llm_usage", "operation_id", "TEXT"),
            ("usage", "operation_id", "TEXT"), ("llm_jobs", "context_json", "TEXT")):
        columns = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
        if name not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    db.commit()


def validate_context(value):
    if value is None:
        return {}
    fields = {"purpose", "course_id", "content_id", "source_id", "client_operation_id"}
    if (not isinstance(value, dict) or set(value) - fields or any(
            not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,160}", item)
            for item in value.values())):
        raise HTTPException(400, "invalid non-secret operation context")
    return value


def retain_direct_upload_job(db, row):
    """Keep observed task metadata, without retaining or reviving upload keys.

    Caller commits this together with removal of the expired authorization.
    A conflicting existing receipt must preserve the grant and fail cleanup;
    receipt data is never inferred from a bill, timestamp or transcript.
    """
    fields = ('id', 'account_id', 'state', 'fingerprint', 'seconds_charged',
              'reused', 'source_id', 'expires_at')
    value = {key: row[key] for key in fields}
    if (not isinstance(value['id'], str) or not re.fullmatch(r'[0-9a-f]{32}', value['id'])
            or type(value['account_id']) is not int or value['account_id'] <= 0
            or value['state'] not in {'issued', 'processing', 'done'}
            or (value['fingerprint'] is not None and (not isinstance(value['fingerprint'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', value['fingerprint'])))
            or (value['seconds_charged'] is not None and (type(value['seconds_charged']) is not int
                or value['seconds_charged'] < 0))
            or (value['reused'] is not None and (type(value['reused']) is not int or value['reused'] not in (0, 1)))
            or type(value['expires_at']) not in (int, float)
            or not math.isfinite(value['expires_at']) or value['expires_at'] <= 0):
        raise ValueError('invalid observed ASR receipt metadata')
    if value['source_id'] is not None:
        validate_context({'source_id': value['source_id']})
    existing = db.execute('SELECT '+','.join(fields)+' FROM asr_job_receipts WHERE id=?',
                          (value['id'],)).fetchone()
    if existing is not None:
        if dict(existing) != value:
            raise ValueError('immutable ASR receipt conflict')
        return
    db.execute('INSERT INTO asr_job_receipts('+','.join(fields)+',retained_at) VALUES(?,?,?,?,?,?,?,?,?)',
               (*[value[key] for key in fields], time.time()))


@contextmanager
def oss_context(db_path, account_id, operation_id, source_id=None):
    token = _OSS_CONTEXT.set((str(db_path), account_id, operation_id, source_id))
    try:
        yield
    finally:
        _OSS_CONTEXT.reset(token)


def record_object(key, event, *, exists=None, size=None):
    context = _OSS_CONTEXT.get()
    if context is None:
        return
    if not key.startswith("pku-temp/") or ".." in key.split("/"):
        raise ValueError("OSS evidence must refer to temporary audio")
    db_path, account_id, operation_id, source_id = context
    with sqlite3.connect(db_path, timeout=5) as db:
        db.execute("INSERT INTO oss_object_events(account_id,operation_id,object_key,event,"
                   "observed_at,observed_exists,bytes,source_id) VALUES (?,?,?,?,?,?,?,?)",
                   (account_id, operation_id, key, event, time.time(),
                    None if exists is None else int(exists), size, source_id))


def head_object(store, key):
    try:
        result = store.bucket.head_object(key)
    except Exception as exc:
        # Network/permission failures are unknown; only actual OSS 404 proves absence.
        if getattr(exc, "status", None) == 404:
            return {"exists": False, "status": 404, "bytes": None}
        return {"exists": None, "status": getattr(exc, "status", None), "bytes": None}
    status = getattr(result, "status", None)
    return {"exists": True if status == 200 else None, "status": status,
            "bytes": getattr(result, "content_length", None) if status == 200 else None}


def _fresh_heads(store, keys):
    """Bound concurrency/queued work; every successful snapshot reads each key.

    No database connections or account credentials enter these workers. A
    deadline or an unknown HEAD fails the entire snapshot, never substitutes
    a cached status. Cancel pending work while in-flight SDK calls retain the
    fixed global worker cap until their own timeout finishes.
    """
    keys = iter(keys)
    pending, observed = {}, {}
    deadline = time.monotonic() + HEAD_TIMEOUT_SECONDS
    exhausted = False
    try:
        while pending or not exhausted:
            if time.monotonic() >= deadline:
                raise HTTPException(503, 'OSS evidence observation deadline exceeded')
            while not exhausted and len(pending) < HEAD_WORKERS:
                try:
                    key = next(keys)
                except StopIteration:
                    exhausted = True
                    break
                if not _HEAD_SLOTS.acquire(timeout=max(0, deadline - time.monotonic())):
                    raise HTTPException(503, 'OSS evidence observation deadline exceeded')
                try:
                    future = _HEAD_POOL.submit(head_object, store, key)
                except Exception:
                    _HEAD_SLOTS.release()
                    raise
                future.add_done_callback(lambda finished: _HEAD_SLOTS.release())
                pending[future] = key
            if not pending:
                break
            done, _ = wait(pending, timeout=max(0, deadline - time.monotonic()), return_when=FIRST_COMPLETED)
            if not done:
                raise HTTPException(503, 'OSS evidence observation deadline exceeded')
            for future in done:
                key = pending.pop(future)
                value = future.result()
                if value['exists'] is None:
                    raise HTTPException(503, 'OSS object state could not be independently read')
                observed[key] = value
        return observed
    finally:
        for future in pending:
            future.cancel()


def _rows(db, query, account):
    values = [dict(row) for row in db.execute(query, (account,)).fetchmany(MAX_RECORDS + 1)]
    if len(values) > MAX_RECORDS:
        raise HTTPException(503, "account evidence exceeds unpaginated read bound")
    return values


def billing(db, account):
    # Both aggregates use this route's same read transaction; no quota reset or writes.
    seconds = db.execute("SELECT COALESCE(SUM(seconds),0) FROM usage WHERE account_id=? "
                         "AND kind='transcribe'", (account,)).fetchone()[0]
    millipoints = db.execute("SELECT COALESCE(SUM(millipoints),0) FROM llm_usage "
                            "WHERE account_id=?", (account,)).fetchone()[0]
    return {"schema": 1, "charged_seconds": seconds, "charged_points": millipoints / 1000,
            "source": "account_cumulative_usage_rows", "observed_at": time.time()}


def _operation_snapshot(db, account):
    """Materialize all account rows in the caller's single SQLite snapshot."""
    jobs = _rows(db, "SELECT job_id AS id,job_id AS operation_id,operation,status,"
                 "points_charged,created_at,context_json FROM llm_jobs WHERE account_id=? ORDER BY created_at,job_id", account)
    for row in jobs:
        context = json.loads(row.pop("context_json") or "{}")
        for key in ("purpose", "course_id", "content_id", "source_id", "client_operation_id"):
            if key in context:
                row[key] = context[key]
    settlements = _rows(db, "SELECT 'llm:'||id AS id,operation_id,operation,"
        "millipoints/1000.0 AS charged_points,recorded_at FROM llm_usage WHERE account_id=? ORDER BY id", account)
    settlements += _rows(db, "SELECT 'asr:'||id AS id,operation_id,seconds AS charged_seconds,"
        "recorded_at FROM usage WHERE account_id=? AND kind='transcribe' ORDER BY id", account)
    # Legacy usage exists and is counted, but absent job correlation remains null.
    # Never manufacture a correlation from timestamps or an account's balance.
    grants = _rows(db, "SELECT id,'asr:'||id AS operation_id,state AS status,fingerprint,"
        "seconds_charged,reused FROM ("
        "SELECT id,account_id,state,fingerprint,seconds_charged,reused FROM direct_upload_grants "
        "UNION ALL SELECT r.id,r.account_id,r.state,r.fingerprint,r.seconds_charged,r.reused "
        "FROM asr_job_receipts r WHERE NOT EXISTS (SELECT 1 FROM direct_upload_grants g WHERE g.id=r.id)"
        ") WHERE account_id=? ORDER BY id", account)
    jobs += grants
    observed = _rows(db, "SELECT id,'asr:'||id AS operation_id,state AS status,fingerprint,"
        "seconds_charged,reused FROM asr_observed_jobs WHERE account_id=? ORDER BY id", account)
    jobs += observed
    observations = _rows(db, "SELECT 'asr:'||id AS operation_id,original_body_sha256,original_body_bytes,"
        "observed_at,restored_at FROM asr_observed_jobs WHERE account_id=? ORDER BY id", account)
    by_operation = {}
    for row in jobs:
        by_operation.setdefault(row['operation_id'], []).append(row)
    gaps = []
    for op in sorted({row['operation_id'] for row in settlements if row.get('operation_id')}):
        count = len(by_operation.get(op, []))
        if count != 1:
            gaps.append({'operation_id': op, 'reason': 'missing_job' if count == 0 else 'ambiguous_job'})
    submissions = _rows(db, "SELECT operation_id AS id,operation_id,course_id,content_id,state,"
        "file_sha256 FROM assignment_submissions WHERE account_id=? "
        "AND state IN ('verification_pending','submitted') ORDER BY course_id,content_id,operation_id", account)
    history = _rows(db, "SELECT id,operation_id,object_key,event,observed_at,observed_exists,bytes,"
        "source_id FROM oss_object_events WHERE account_id=? ORDER BY id", account)
    redemptions = _rows(db, "SELECT rowid AS id,transcribe_seconds,llm_millipoints,redeemed_at "
        "FROM codes WHERE redeemed_by=? ORDER BY rowid", account)
    return {**billing(db, account), "jobs": jobs, "settlements": settlements,
            "submission_posts": submissions, "submission_posts_source": "account_armed_submission_permits",
            "submissions": len(submissions), "oss_objects": [], "oss_history": history,
            "redemptions": redemptions, "complete": not gaps, "correlation_gaps": gaps,
            "archived_job_observations": observations,
            "unattributed_settlement_count": sum(not row.get('operation_id') for row in settlements)}


def _resolve_operations(snapshot, store):
    """Fresh OSS observations after the route releases its SQLite read lock."""
    if store is None:
        raise HTTPException(503, "real OSS HEAD oracle unavailable")
    history = snapshot['oss_history']
    live = []
    grouped = {}
    for row in history:
        grouped[row["object_key"]] = row
        row["object_key_sha256"] = hashlib.sha256(row["object_key"].encode()).hexdigest()
    heads = _fresh_heads(store, grouped)
    for key, latest in grouped.items():
        observed = heads[key]
        item = {"id": hashlib.sha256(key.encode()).hexdigest(), "operation_id": latest["operation_id"],
                "source_id": latest["source_id"], "head": observed, "observed_at": time.time()}
        if observed["exists"]:
            live.append(item)
        latest["current_head"] = observed
    for row in history:
        row.pop("object_key")
    snapshot['oss_objects'] = live
    return snapshot


def operations(db, account, store):
    if store is None:
        raise HTTPException(503, "real OSS HEAD oracle unavailable")
    return _resolve_operations(_operation_snapshot(db, account), store)


def register_routes(app, account_for, require_verified):
    def read(authorization, include_operations):
        path = Path(app.state.db_path).resolve()
        try:
            db = sqlite3.connect("file:" + quote(path.as_posix(), safe="/:") + "?mode=ro", uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                account = account_for(authorization, db)
                require_verified(account)
                if include_operations:
                    snapshot = _operation_snapshot(db, account['id'])
                else:
                    snapshot = billing(db, account['id'])
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise HTTPException(503, "account evidence schema unavailable") from exc
        if include_operations:
            vendor = app.state.streaming_asr_fn
            return _resolve_operations(snapshot, getattr(vendor, 'store', None))
        return snapshot

    @app.get("/v1/billing/ledger")
    def billing_ledger(authorization: str | None = Header(default=None)):
        return read(authorization, False)

    @app.get("/v1/operations")
    def operation_ledger(authorization: str | None = Header(default=None)):
        return read(authorization, True)
