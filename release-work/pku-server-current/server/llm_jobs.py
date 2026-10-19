"""Durable, account-scoped LLM jobs for calls longer than the proxy timeout.

Prompts live only in the worker's memory. Completed answers are retained in
SQLite so a client can recover a result after a network timeout without
repeating the vendor call or its charge. An interrupted job fails closed on
the next process start; the client may start a new logical job explicitly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Callable

import httpx

from . import accounts
from .llm import Completion, LlmError


SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_jobs (
    job_id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    operation TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'qwen3.5-flash',
    status TEXT NOT NULL,
    content TEXT,
    points_charged REAL,
    error TEXT,
    error_status INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (account_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_llm_jobs_account ON llm_jobs (account_id, job_id);
CREATE INDEX IF NOT EXISTS idx_llm_jobs_status_age ON llm_jobs (status, updated_at);
"""

RESULT_TTL_SECONDS = 30 * 24 * 3600
MAX_VENDOR_ATTEMPTS = 4
MAX_PERSISTENCE_ATTEMPTS = 4


def _persistence_pause(seconds: float) -> None:
    time.sleep(seconds)


def _persist_with_retry(db_path: Path, action: Callable) -> object:
    """Retry only SQLite lock contention, using fresh connections.

    The action must be transactional and idempotent. In particular, _finish
    checks running before settling, so an ambiguous completed commit cannot
    debit again. A vendor completion remains in memory throughout these
    bounded attempts; persistence contention never calls the vendor again.
    """
    for attempt in range(MAX_PERSISTENCE_ATTEMPTS):
        conn = None
        try:
            conn = accounts.connect(db_path)
            return action(conn)
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            locked = (code is not None and code & 255 in
                      (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
            if code is None:
                locked = str(exc) in ("database is locked", "database table is locked")
            if not locked or attempt == MAX_PERSISTENCE_ATTEMPTS - 1:
                raise
        finally:
            if conn is not None:
                conn.close()
        _persistence_pause(min(.25 * 2 ** attempt, 1.0))


def _worker_finished(db_path: Path, job_id: str, account_id: int, future) -> None:
    """A completed executor task cannot silently retain a running DB row.

    This runs only after that exact Future finishes, never on an age timeout
    while a vendor request could still be active. Terminal/settled jobs and
    other accounts are left intact. No prompts or exception messages are logged.
    """
    if not future.cancelled():
        error = future.exception()
        if error is not None:
            logging.getLogger(__name__).error(
                "AI worker ended unexpectedly (%s)", type(error).__name__)
    def reconcile(conn):
        row = get(conn, account_id, job_id)
        if row is not None and row["status"] in ("queued", "running"):
            _fail(conn, job_id, "AI operation interrupted before settlement", 503)
    try:
        _persist_with_retry(db_path, reconcile)
    except Exception as exc:
        logging.getLogger(__name__).error(
            "AI worker terminal persistence unavailable (%s)", type(exc).__name__)


def submit_job(executor, db_path: Path, job_id: str, account_id: int,
               operation: str, system: str, user: str, llm_fn: Callable):
    future = executor.submit(run_job, db_path, job_id, account_id,
                             operation, system, user, llm_fn)
    future.add_done_callback(
        lambda completed: _worker_finished(db_path, job_id, account_id, completed))
    return future


def _pause_before_retry(seconds: int) -> None:
    time.sleep(seconds)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(llm_jobs)")}
    if "model" not in columns:
        conn.execute("ALTER TABLE llm_jobs ADD COLUMN model TEXT NOT NULL DEFAULT 'qwen3.5-flash'")


def cleanup_expired(conn: sqlite3.Connection) -> None:
    """Discard old answers but keep key tombstones to prevent recharging."""
    with conn:
        conn.execute(
            "UPDATE llm_jobs SET status = 'failed', content = NULL,"
            " error = 'AI result expired', error_status = 410"
            " WHERE status = 'succeeded' AND updated_at < ?",
            (time.time() - RESULT_TTL_SECONDS,),
        )


def cleanup_expired_db(db_path: Path) -> None:
    conn = accounts.connect(db_path)
    try:
        cleanup_expired(conn)
    finally:
        conn.close()


def request_hash(operation: str, system: str, user: str,
                 model: str = "qwen3.5-flash") -> str:
    # Preserve the hash of legacy/default jobs across the schema upgrade.
    fields = [operation, system, user]
    if model != "qwen3.5-flash":
        fields.append(model)
    payload = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_or_get(
    conn: sqlite3.Connection, account_id: int, key: str, digest: str, operation: str,
    model: str = "qwen3.5-flash", *, operation_context=None,
) -> tuple[sqlite3.Row, bool]:
    now = time.time()
    job_id = secrets.token_hex(16)
    with conn:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO llm_jobs"
            " (job_id, account_id, idempotency_key, request_hash, operation, model,"
            " status, created_at, updated_at, context_json) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
            (job_id, account_id, key, digest, operation, model, now, now, json.dumps(operation_context or {}, sort_keys=True)),
        ).rowcount == 1
        row = conn.execute(
            "SELECT * FROM llm_jobs WHERE account_id = ? AND idempotency_key = ?",
            (account_id, key),
        ).fetchone()
    assert row is not None
    return row, inserted


def get(conn: sqlite3.Connection, account_id: int, job_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM llm_jobs WHERE account_id = ? AND job_id = ?",
        (account_id, job_id),
    ).fetchone()


def get_by_key(conn: sqlite3.Connection, account_id: int, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM llm_jobs WHERE account_id = ? AND idempotency_key = ?",
        (account_id, key),
    ).fetchone()


def public_result(row: sqlite3.Row) -> dict:
    result = {"job_id": row["job_id"], "status": row["status"]}
    if row["status"] == "succeeded":
        result.update(content=row["content"], points_charged=row["points_charged"])
    elif row["status"] == "failed":
        result.update(error=row["error"], error_status=row["error_status"])
    return result


def recover_interrupted(conn: sqlite3.Connection) -> None:
    """Single-active-server deployment: no worker survives a process restart."""
    with conn:
        conn.execute(
            "UPDATE llm_jobs SET status = 'failed', error = 'AI operation interrupted',"
            " error_status = 503, updated_at = ? WHERE status IN ('queued', 'running')",
            (time.time(),),
        )


def _fail(conn: sqlite3.Connection, job_id: str, error: str, status: int) -> None:
    with conn:
        conn.execute(
            "UPDATE llm_jobs SET status = 'failed', error = ?, error_status = ?,"
            " updated_at = ? WHERE job_id = ? AND status IN ('queued', 'running')",
            (error, status, time.time(), job_id),
        )


def fail_queued(conn: sqlite3.Connection, job_id: str) -> None:
    _fail(conn, job_id, "AI jobs unavailable", 503)


def _finish(conn: sqlite3.Connection, job_id: str, account_id: int, operation: str,
            completion: Completion) -> None:
    """Settle the account, usage row, and recoverable result atomically."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = get(conn, account_id, job_id)
        if row is None or row["status"] != "running":
            conn.rollback()
            return
        if completion.model != row["model"]:
            raise ValueError("AI job completion model mismatch")
        bundled = accounts.is_bundled_llm_operation(conn, account_id, operation)
        charged_milli = 0 if bundled else completion.millipoints
        if charged_milli > 0:
            updated = conn.execute(
                "UPDATE accounts SET llm_millipoints_remaining = llm_millipoints_remaining - ?"
                " WHERE id = ? AND llm_millipoints_remaining >= ?",
                (charged_milli, account_id, charged_milli),
            )
            ok = updated.rowcount == 1
        else:
            ok = conn.execute(
                "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
            ).fetchone() is not None
        if not ok:
            conn.execute(
                "UPDATE llm_jobs SET status = 'failed', error = 'AI points exhausted',"
                " error_status = 402, updated_at = ? WHERE job_id = ?",
                (time.time(), job_id),
            )
        else:
            conn.execute(
                "INSERT INTO llm_usage (account_id, operation, model, input_tokens,"
                " output_tokens, millipoints, recorded_at, operation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, operation, completion.model, completion.input_tokens,
                 completion.output_tokens, charged_milli, time.time(), job_id),
            )
            conn.execute(
                "UPDATE llm_jobs SET status = 'succeeded', content = ?,"
                " points_charged = ?, updated_at = ? WHERE job_id = ?",
                (completion.content, charged_milli / 1000, time.time(), job_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def run_job(db_path: Path, job_id: str, account_id: int, operation: str,
            system: str, user: str, llm_fn: Callable[[str, str, str], Completion]) -> None:
    def claim(conn):
        with conn:
            return conn.execute(
                "UPDATE llm_jobs SET status = 'running', updated_at = ?"
                " WHERE job_id = ? AND account_id = ? AND status = 'queued'",
                (time.time(), job_id, account_id),
            ).rowcount
    if not _persist_with_retry(db_path, claim):
        return
    # Do not retain a connection during the long vendor call.
    try:
        connect_timeouts = 0
        other_transport_errors = 0
        for attempt in range(MAX_VENDOR_ATTEMPTS):
            try:
                completion = llm_fn(operation, system, user)
                break
            except httpx.ConnectTimeout:
                connect_timeouts += 1
                if attempt == MAX_VENDOR_ATTEMPTS - 1:
                    raise
                _pause_before_retry(min(2 ** connect_timeouts, 8))
            except httpx.TransportError:
                other_transport_errors += 1
                if other_transport_errors >= 2 or attempt == MAX_VENDOR_ATTEMPTS - 1:
                    raise
                _pause_before_retry(2)
    except LlmError:
        _persist_with_retry(db_path, lambda conn: _fail(
            conn, job_id, "AI operation failed upstream", 502))
        return
    except Exception as exc:
        logging.getLogger(__name__).error("AI job failed (%s)", type(exc).__name__)
        _persist_with_retry(db_path, lambda conn: _fail(
            conn, job_id, "AI operation failed", 502))
        return
    try:
        _persist_with_retry(db_path, lambda conn: _finish(
            conn, job_id, account_id, operation, completion))
    except Exception as exc:
        logging.getLogger(__name__).error(
            "AI job settlement failed (%s)", type(exc).__name__)
        _persist_with_retry(db_path, lambda conn: _fail(
            conn, job_id, "AI operation failed", 502))
