"""Account/course canonical teaching-portal snapshots and projection locks.

The database allocates revisions under BEGIN IMMEDIATE. Incomplete reads and
one-off omissions cannot replace a complete snapshot. No client timestamps
participate in ordering. Projection locks deliberately fail closed on crashes.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time

from .catalog_schema import SECTIONS, ASSESSMENT_KINDS, fingerprint, validate_snapshot

OBSERVATION_TTL = 1800
SCHEMA = """
CREATE TABLE IF NOT EXISTS course_catalog (
 account_id INTEGER NOT NULL, course_id TEXT NOT NULL, revision INTEGER NOT NULL,
 snapshot_json TEXT NOT NULL, permit_id TEXT, PRIMARY KEY (account_id, course_id)
);
CREATE TABLE IF NOT EXISTS catalog_observations (
 id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, course_id TEXT NOT NULL,
 revision INTEGER NOT NULL, expires_at REAL NOT NULL, accepted_hash TEXT,
 accepted_revision INTEGER
);
CREATE TABLE IF NOT EXISTS assignment_submissions (
 account_id INTEGER NOT NULL, course_id TEXT NOT NULL, content_id TEXT NOT NULL,
 portal_account_sha256 TEXT NOT NULL, operation_id TEXT NOT NULL,
 revision INTEGER NOT NULL, file_sha256 TEXT NOT NULL, permit_id TEXT NOT NULL,
 state TEXT NOT NULL, receipt_json TEXT,
 PRIMARY KEY (account_id, course_id, content_id, portal_account_sha256, operation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS assignment_submission_active
 ON assignment_submissions(account_id, course_id, content_id, portal_account_sha256)
 WHERE state IN ('acquired', 'verification_pending');
"""


class Conflict(ValueError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _revision(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid revision")


def _course(value):
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ValueError("invalid course identity")


def get(conn: sqlite3.Connection, account_id: int, course_id: str) -> dict:
    _course(course_id)
    row = conn.execute("SELECT revision, snapshot_json FROM course_catalog WHERE account_id=? AND course_id=?",
                       (account_id, course_id)).fetchone()
    return {"revision": int(row["revision"]), "snapshot": json.loads(row["snapshot_json"])} if row else {"revision": 0, "snapshot": None}


def list_courses(conn: sqlite3.Connection, account_id: int) -> dict:
    rows = conn.execute("SELECT revision, snapshot_json FROM course_catalog WHERE account_id=? ORDER BY course_id",
                        (account_id,)).fetchall()
    return {"courses": [{"revision": int(row["revision"]), "snapshot": json.loads(row["snapshot_json"])} for row in rows]}


def observe(conn: sqlite3.Connection, account_id: int, course_id: str) -> dict:
    _course(course_id)
    now = time.time()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current = get(conn, account_id, course_id)
        identifier = secrets.token_hex(32)
        conn.execute("DELETE FROM catalog_observations WHERE expires_at < ?", (now,))
        conn.execute("INSERT INTO catalog_observations(id, account_id, course_id, revision, expires_at) VALUES (?,?,?,?,?)",
                     (identifier, account_id, course_id, current["revision"], now + OBSERVATION_TTL))
    return {"revision": current["revision"], "observation_id": identifier}


def put(conn: sqlite3.Connection, account_id: int, course_id: str, revision: int,
        observation_id: str, snapshot: object, *, removal_confirmation=None) -> dict:
    _revision(revision)
    _course(course_id)
    snapshot = validate_snapshot(snapshot)
    if snapshot["course_id"] != course_id:
        raise ValueError("course identity mismatch")
    digest = fingerprint(snapshot)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current = get(conn, account_id, course_id)
        ticket = conn.execute("SELECT * FROM catalog_observations WHERE id=? AND account_id=? AND course_id=?",
                              (observation_id, account_id, course_id)).fetchone()
        if ticket is None or ticket["revision"] != revision or ticket["expires_at"] < time.time():
            raise Conflict("observation expired or unknown; fetch teaching portal again")
        if ticket["accepted_hash"] is not None:
            if (ticket["accepted_hash"] == digest and ticket["accepted_revision"] == current["revision"]):
                return current
            raise Conflict("observation already consumed")
        if revision != current["revision"]:
            raise Conflict("stale course revision; do not rebase cached data")
        stored = conn.execute("SELECT permit_id FROM course_catalog WHERE account_id=? AND course_id=?",
                              (account_id, course_id)).fetchone()
        if stored and stored["permit_id"]:
            raise Conflict("shared projection in progress")
        confirmation_id = None
        if removal_confirmation is not None:
            confirmation_id = _confirm_source_replacement(conn, account_id, course_id, revision,
                                                         observation_id, current, snapshot, removal_confirmation)
        if current["snapshot"]:
            for section in SECTIONS:
                previous_ids = {row["source_id"] for row in current["snapshot"]["sections"][section]["items"]}
                next_ids = {row["source_id"] for row in snapshot["sections"][section]["items"]}
                missing = previous_ids - next_ids
                if section == 'assignments':
                    missing -= _corrected_assessment_mappings(current['snapshot'], snapshot)
                if missing and confirmation_id is None:
                    raise Conflict("missing source IDs need explicit portal removal review")
        next_revision = revision + 1
        conn.execute("INSERT INTO course_catalog(account_id,course_id,revision,snapshot_json) VALUES (?,?,?,?) "
                     "ON CONFLICT(account_id,course_id) DO UPDATE SET revision=excluded.revision, snapshot_json=excluded.snapshot_json",
                     (account_id, course_id, next_revision, json.dumps(snapshot, ensure_ascii=False)))
        conn.execute("UPDATE catalog_observations SET accepted_hash=?, accepted_revision=? WHERE id=?",
                     (digest, next_revision, observation_id))
        if confirmation_id:
            conn.execute("UPDATE catalog_observations SET accepted_hash=?, accepted_revision=? WHERE id=?",
                         (digest, next_revision, confirmation_id))
    return {"revision": next_revision, "snapshot": snapshot}



def _confirm_source_replacement(conn, account_id, course_id, revision, observation_id,
                                current, snapshot, confirmation):
    """An explicit replacement needs a second live read bound to the exact base."""
    fields = {'observation_id', 'base_snapshot_sha256', 'snapshot_sha256'}
    if not isinstance(confirmation, dict) or set(confirmation) != fields:
        raise ValueError('invalid source replacement confirmation')
    identifier = confirmation['observation_id']
    if (not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier)
            or identifier == observation_id):
        raise ValueError('distinct confirmation observation required')
    if (current['snapshot'] is None
            or confirmation['base_snapshot_sha256'] != fingerprint(current['snapshot'])
            or confirmation['snapshot_sha256'] != fingerprint(snapshot)):
        raise Conflict('source replacement base or confirmed snapshot changed')
    ticket = conn.execute('SELECT * FROM catalog_observations WHERE id=? AND account_id=? AND course_id=?',
                          (identifier, account_id, course_id)).fetchone()
    if (ticket is None or ticket['revision'] != revision or ticket['expires_at'] < time.time()
            or ticket['accepted_hash'] is not None):
        raise Conflict('source replacement confirmation expired, consumed or belongs to another identity')
    return identifier



def _corrected_assessment_mappings(previous, fresh):
    """Retire only wrong upload mappings; their stable official content remains."""
    old_materials = previous['sections']['materials']['items']
    new_materials = fresh['sections']['materials']['items']
    corrected = set()
    for assignment in previous['sections']['assignments']['items']:
        if assignment.get('source') != 'content-tree' or not assignment.get('content_id'):
            continue
        old = [item for item in old_materials if item.get('content_id') == assignment['content_id']]
        new = [item for item in new_materials if item.get('content_id') == assignment['content_id']]
        if (len(old) == len(new) == 1 and old[0].get('kind') in ASSESSMENT_KINDS
                and new[0].get('kind') in ASSESSMENT_KINDS and old[0]['source_id'] == new[0]['source_id']):
            corrected.add(assignment['source_id'])
    return corrected


def acquire(conn: sqlite3.Connection, account_id: int, course_id: str, revision: int) -> dict:
    _revision(revision)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current = get(conn, account_id, course_id)
        if revision < 1 or current["revision"] != revision:
            raise Conflict("stale projection revision")
        permit_id = secrets.token_hex(32)
        result = conn.execute("UPDATE course_catalog SET permit_id=? WHERE account_id=? AND course_id=? AND revision=? AND permit_id IS NULL",
                              (permit_id, account_id, course_id, revision))
        if result.rowcount != 1:
            raise Conflict("another shared projection is running")
    return {"permit_id": permit_id, "revision": revision}


def verify_permit(conn: sqlite3.Connection, account_id: int, course_id: str,
                  revision: int, permit_id: str) -> dict:
    _revision(revision)
    if not isinstance(permit_id, str) or not permit_id:
        raise ValueError("invalid permit")
    row = conn.execute("SELECT revision, permit_id FROM course_catalog WHERE account_id=? AND course_id=?",
                       (account_id, course_id)).fetchone()
    if row is None or row["revision"] != revision or row["permit_id"] != permit_id:
        raise Conflict("projection permit mismatch")
    return {"verified": True, "revision": revision}


def release(conn: sqlite3.Connection, account_id: int, course_id: str, permit_id: str) -> dict:
    if not isinstance(permit_id, str) or not permit_id:
        raise ValueError("invalid permit")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        result = conn.execute("UPDATE course_catalog SET permit_id=NULL WHERE account_id=? AND course_id=? AND permit_id=?",
                              (account_id, course_id, permit_id))
        if result.rowcount != 1:
            raise Conflict("projection permit mismatch")
    return {"released": True}


def _submission_identity(course_id, content_id, owner, operation):
    if (not isinstance(course_id, str) or not re.fullmatch(r'_\d+_\d+', course_id)
            or not isinstance(content_id, str) or not re.fullmatch(r'_\d+_\d+', content_id)
            or not isinstance(owner, str) or not re.fullmatch(r'[0-9a-f]{64}', owner)
            or not isinstance(operation, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', operation)):
        raise ValueError('invalid submission identity')


def _submission_row(conn, account, course, content, owner, operation, permit=None):
    _submission_identity(course, content, owner, operation)
    row = conn.execute('SELECT * FROM assignment_submissions WHERE account_id=? AND course_id=? '
                       'AND content_id=? AND portal_account_sha256=? AND operation_id=?',
                       (account, course, content, owner, operation)).fetchone()
    if row is None or (permit is not None and row['permit_id'] != permit):
        raise Conflict('submission permit mismatch')
    return row


def _submission_result(row):
    result = {key: row[key] for key in ('operation_id', 'permit_id', 'revision', 'file_sha256', 'state')}
    if row['receipt_json']:
        result['receipt'] = json.loads(row['receipt_json'])
    return result


def get_submission(conn, account, course, content, owner, operation):
    """Read an existing operation; recovery must never acquire a fresh one."""
    return _submission_result(_submission_row(conn, account, course, content, owner, operation))


def begin_submission(conn, account, course, content, revision, owner, operation, digest):
    """Acquire once per target; replay returns state, never grants another POST."""
    _submission_identity(course, content, owner, operation)
    _revision(revision)
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('invalid file digest')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM assignment_submissions WHERE account_id=? AND course_id=? '
                           'AND content_id=? AND portal_account_sha256=? AND operation_id=?',
                           (account, course, content, owner, operation)).fetchone()
        if row:
            if row['file_sha256'] != digest or row['revision'] != revision or row['state'] == 'aborted':
                raise Conflict('submission operation cannot be reused')
            return _submission_result(row)
        current = get(conn, account, course)
        if revision < 1 or current['revision'] != revision:
            raise Conflict('submission requires the latest complete course revision')
        matches = [item for item in current['snapshot']['sections']['assignments']['items']
                   if item.get('content_id') == content and item.get('source') != 'calendar']
        is_assessment = any(item.get('content_id') == content and item.get('kind') in ASSESSMENT_KINDS
                            for item in current['snapshot']['sections']['materials']['items'])
        if len(matches) != 1 or is_assessment:
            raise Conflict('no unique official assignment mapping')
        active = conn.execute('SELECT 1 FROM assignment_submissions WHERE account_id=? AND course_id=? '
                             "AND content_id=? AND portal_account_sha256=? AND state IN ('acquired','verification_pending')",
                             (account, course, content, owner)).fetchone()
        if active:
            raise Conflict('another submission is active or awaiting portal verification')
        conn.execute('INSERT INTO assignment_submissions VALUES (?,?,?,?,?,?,?,?,?,NULL)',
                     (account, course, content, owner, operation, revision, digest, secrets.token_urlsafe(32), 'acquired'))
        return _submission_result(_submission_row(conn, account, course, content, owner, operation))


def mark_submission_posted(conn, account, course, content, owner, operation, permit):
    """Durably fence before POST; a lost reply must never authorize a retry."""
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _submission_row(conn, account, course, content, owner, operation, permit)
        if row['state'] != 'acquired':
            raise Conflict('submission request has already been armed or completed')
        # Refreshes can invalidate a prepared target before the request is armed.
        if get(conn, account, course)['revision'] != row['revision']:
            raise Conflict('course revision changed before submission')
        conn.execute("UPDATE assignment_submissions SET state='verification_pending' WHERE permit_id=? AND account_id=?",
                     (permit, account))
        return _submission_result(_submission_row(conn, account, course, content, owner, operation, permit))


def release_submission(conn, account, course, content, owner, operation, permit):
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _submission_row(conn, account, course, content, owner, operation, permit)
        if row['state'] not in ('acquired', 'aborted'):
            raise Conflict('possibly sent requests cannot be released')
        conn.execute("UPDATE assignment_submissions SET state='aborted' WHERE permit_id=? AND account_id=?", (permit, account))
    return {'released': True}


def complete_submission(conn, account, course, content, owner, operation, permit, receipt):
    """Store authenticated client verification metadata, never portal HTML/tokens."""
    fields = {'course_id', 'content_id', 'portal_verified', 'verified_at', 'review_history_sha256'}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or receipt.get('course_id') != course or receipt.get('content_id') != content
            or receipt.get('portal_verified') is not True
            or not isinstance(receipt.get('verified_at'), str) or not 1 <= len(receipt['verified_at']) <= 100
            or not isinstance(receipt.get('review_history_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', receipt['review_history_sha256'])):
        raise ValueError('invalid portal receipt metadata')
    encoded = json.dumps(receipt, sort_keys=True)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _submission_row(conn, account, course, content, owner, operation, permit)
        if row['state'] == 'submitted' and row['receipt_json'] == encoded:
            return _submission_result(row)
        if row['state'] != 'verification_pending':
            raise Conflict('receipt does not belong to a pending request')
        conn.execute("UPDATE assignment_submissions SET state='submitted', receipt_json=? WHERE permit_id=? AND account_id=?",
                     (encoded, permit, account))
        return _submission_result(_submission_row(conn, account, course, content, owner, operation, permit))
