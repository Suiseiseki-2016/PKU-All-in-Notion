"""Durable transcript reuse by exact audio digest or canonical lecture part.

Lecture reuse is shared across accounts by (source_id, part_index), even when
encoded audio bytes differ. Both stores retain transcript content only, with
bounded TTL; raw audio, credentials and account identity are never cached.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from typing import Any, BinaryIO, IO

from .asr import MODEL as ASR_MODEL
from .asr import _HashingReader

DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # bounded retention, matches the quota cycle
FALLBACK_MODEL_VERSION = "dashscope-sdk-unknown"
TTL_ENV = "TRANSCRIPT_REUSE_TTL_SECONDS"
_SPOOL_CHUNK_BYTES = 1024 * 1024
_SPOOL_IN_MEMORY_BYTES = 32 * 1024 * 1024
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


class TranscriptTooLarge(RuntimeError):
    """The spooled upload exceeded the relay's byte cap (VAL-SECU-016).

    Raised mid-spool so an oversized upload is rejected with 413 (the
    route's caller maps it there) without ever buffering or reading the
    rest of the stream.
    """

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_reuse (
    fingerprint_sha256 TEXT PRIMARY KEY NOT NULL,
    transcript_result TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    model TEXT NOT NULL,
    model_version TEXT NOT NULL,
    expires_at REAL NOT NULL
)
"""

_UPSERT = """
INSERT INTO transcript_reuse
    (fingerprint_sha256, transcript_result, created_at, updated_at,
     model, model_version, expires_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(fingerprint_sha256) DO UPDATE SET
    transcript_result = excluded.transcript_result,
    created_at = excluded.created_at,
    updated_at = excluded.updated_at,
    model = excluded.model,
    model_version = excluded.model_version,
    expires_at = excluded.expires_at
"""


LECTURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lecture_transcript_reuse (
    source_id TEXT NOT NULL,
    part_index INTEGER NOT NULL DEFAULT 0,
    fingerprint_sha256 TEXT NOT NULL,
    transcript_result TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (source_id, part_index)
)
"""

_LECTURE_UPSERT = """
INSERT INTO lecture_transcript_reuse
    (source_id, part_index, fingerprint_sha256, transcript_result,
     created_at, updated_at, expires_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source_id, part_index) DO UPDATE SET
    fingerprint_sha256 = excluded.fingerprint_sha256,
    transcript_result = excluded.transcript_result,
    created_at = excluded.created_at,
    updated_at = excluded.updated_at,
    expires_at = excluded.expires_at
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the reuse tables if absent; idempotent on every connect()."""
    conn.execute(SCHEMA)
    conn.execute(LECTURE_SCHEMA)


def is_valid_fingerprint(fingerprint: Any) -> bool:
    """True only for an exact 64-char lowercase hex SHA-256 string."""
    return isinstance(fingerprint, str) and _FINGERPRINT.fullmatch(fingerprint) is not None


def fingerprint_of(audio: bytes) -> str:
    """Exact SHA-256 fingerprint of the uploaded bytes (bytes path)."""
    return hashlib.sha256(audio).hexdigest()


def spool_and_hash(
    source: BinaryIO, max_bytes: int | None = None
) -> tuple[IO[bytes], str]:
    """Spool-first: hash the exact upload bytes into a bounded temp file.

    Reads the raw audio stream once, hashing every chunk with the same
    streaming SHA-256 machinery the OSS upload uses (``_HashingReader``)
    while buffering it in a ``SpooledTemporaryFile``. The returned file
    is rewound, so a miss can hand it straight to the streaming vendor;
    a hit never touches OSS at all. No multipart metadata (filename,
    content type) enters the digest — only the exact bytes.

    ``max_bytes`` caps the accepted upload: the spool aborts as soon as
    the stream passes it (raising :class:`TranscriptTooLarge`), so an
    oversized file is refused instead of being read to the end.
    """
    spooled = tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY_BYTES)
    reader = _HashingReader(source)
    written = 0
    while True:
        chunk = reader.read(_SPOOL_CHUNK_BYTES)
        if not chunk:
            break
        written += len(chunk)
        if max_bytes is not None and written > max_bytes:
            spooled.close()
            raise TranscriptTooLarge(
                f"upload of {written} bytes exceeds the {max_bytes}-byte cap"
            )
        spooled.write(chunk)
    spooled.seek(0)
    return spooled, reader.digest.hexdigest()


def ttl_seconds() -> int:
    """Retention window for new rows; env-overridable, default 30 days."""
    try:
        return int(os.environ.get(TTL_ENV, ""))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def model_version() -> str:
    """DashScope SDK version at store time, or the configured fallback."""
    try:
        import dashscope
    except ImportError:
        return FALLBACK_MODEL_VERSION

    version = getattr(dashscope, "__version__", "")
    return str(version) if version else FALLBACK_MODEL_VERSION


def store_transcript(
    conn: sqlite3.Connection, fingerprint: str, transcript_result: dict[str, Any]
) -> None:
    """Upsert the transcript for an exact fingerprint; second write wins.

    One row per fingerprint ever: a re-transcription (e.g. after expiry)
    replaces payload, timestamps, provenance, and expiry together.
    ``expires_at`` is fixed at write time from the TTL and is never
    extended by hits.
    """
    if not is_valid_fingerprint(fingerprint):
        raise ValueError("fingerprint must be a 64-char lowercase hex sha256")
    now = time.time()
    conn.execute("DELETE FROM transcript_reuse WHERE expires_at <= ?", (now,))
    conn.execute(
        _UPSERT,
        (
            fingerprint,
            json.dumps(transcript_result),
            now,
            now,
            ASR_MODEL,
            model_version(),
            now + ttl_seconds(),
        ),
    )
    conn.commit()


def lookup(conn: sqlite3.Connection, fingerprint: str) -> sqlite3.Row | None:
    """Exact-fingerprint lookup; a live hit refreshes ``updated_at``.

    Only the exact 64-char lowercase digest can hit — uppercase hex,
    truncations, substitutions, empty and non-string shapes all miss,
    with no fuzzy fallback of any kind. ``created_at`` and ``expires_at``
    are immutable on hits; ``updated_at`` records the reuse. Expired
    rows (``expires_at <= now``, mirroring session semantics) are a miss
    and are pruned opportunistically.
    """
    if not is_valid_fingerprint(fingerprint):
        return None
    now = time.time()
    conn.execute("DELETE FROM transcript_reuse WHERE expires_at <= ?", (now,))
    touched = conn.execute(
        "UPDATE transcript_reuse SET updated_at = ?"
        " WHERE fingerprint_sha256 = ? AND expires_at > ?",
        (now, fingerprint, now),
    )
    if touched.rowcount != 1:
        conn.commit()
        return None
    row = conn.execute(
        "SELECT * FROM transcript_reuse WHERE fingerprint_sha256 = ?",
        (fingerprint,),
    ).fetchone()
    conn.commit()
    return row


def store_lecture_part(
    conn: sqlite3.Connection,
    source_id: str,
    part_index: int,
    fingerprint: str,
    transcript_result: dict[str, Any],
) -> None:
    """Store a lecture-part transcript keyed by canonical (source_id, part_index) for same-course classmate reuse."""
    clean_source = (source_id or "").strip()
    if not clean_source or len(clean_source) > 200 or isinstance(part_index, bool) or not isinstance(part_index, int) or part_index < 0:
        return
    if not is_valid_fingerprint(fingerprint):
        return
    now = time.time()
    conn.execute("DELETE FROM lecture_transcript_reuse WHERE expires_at <= ?", (now,))
    conn.execute(
        _LECTURE_UPSERT,
        (
            clean_source,
            int(part_index),
            fingerprint,
            json.dumps(transcript_result),
            now,
            now,
            now + ttl_seconds(),
        ),
    )
    conn.commit()


def lookup_lecture_part(
    conn: sqlite3.Connection, source_id: str, part_index: int = 0
) -> sqlite3.Row | None:
    """Look up a non-expired lecture-part transcript by (source_id, part_index)."""
    clean_source = (source_id or "").strip()
    if not clean_source or len(clean_source) > 200 or isinstance(part_index, bool) or not isinstance(part_index, int) or part_index < 0:
        return None
    now = time.time()
    conn.execute("DELETE FROM lecture_transcript_reuse WHERE expires_at <= ?", (now,))
    touched = conn.execute(
        "UPDATE lecture_transcript_reuse SET updated_at = ?"
        " WHERE source_id = ? AND part_index = ? AND expires_at > ?",
        (now, clean_source, int(part_index), now),
    )
    if touched.rowcount != 1:
        conn.commit()
        return None
    row = conn.execute(
        "SELECT * FROM lecture_transcript_reuse WHERE source_id = ? AND part_index = ?",
        (clean_source, int(part_index)),
    ).fetchone()
    conn.commit()
    return row


def prune_expired(conn: sqlite3.Connection) -> int:
    """Delete expired rows (``expires_at <= now``); return the count removed."""
    now = time.time()
    pruned = conn.execute(
        "DELETE FROM transcript_reuse WHERE expires_at <= ?", (now,)
    )
    conn.execute(
        "DELETE FROM lecture_transcript_reuse WHERE expires_at <= ?", (now,)
    )
    conn.commit()
    return int(pruned.rowcount)
