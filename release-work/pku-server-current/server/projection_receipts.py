"""Account-scoped non-expiring projection request receipts.

This candidate does not alter or adopt legacy permits. Stable request IDs
recover acquisition/release responses; an acquired writer remains fenced.
"""
import re
import secrets
import time

from . import catalog

PROTOCOL = {'protocol': 1, 'request_identity': 'client_request_id',
            'release': 'idempotent', 'expiry': None}
SCHEMA = '''CREATE TABLE IF NOT EXISTS catalog_projection_receipts (
 account_id INTEGER NOT NULL, course_id TEXT NOT NULL,
 client_request_id TEXT NOT NULL, revision INTEGER NOT NULL,
 permit_id TEXT NOT NULL, state TEXT NOT NULL,
 created_at REAL NOT NULL, released_at REAL,
 PRIMARY KEY(account_id,course_id,client_request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS catalog_projection_receipt_permit
 ON catalog_projection_receipts(account_id,permit_id);
'''


def ensure_schema(db):
    db.executescript(SCHEMA)


def _identity(course_id, request_id):
    catalog._course(course_id)
    if not isinstance(request_id, str) or not re.fullmatch('[0-9a-f]{64}', request_id):
        raise ValueError('invalid projection request identity')


def _row(db, account_id, course_id, request_id):
    return db.execute('SELECT * FROM catalog_projection_receipts WHERE account_id=? AND course_id=? AND client_request_id=?',
                      (account_id, course_id, request_id)).fetchone()


def _result(row):
    return {key: row[key] for key in ('client_request_id', 'revision', 'permit_id', 'state')}


def _verify_active(db, account_id, course_id, row):
    if row['state'] != 'acquired':
        raise catalog.Conflict('projection request already released')
    catalog.verify_permit(db, account_id, course_id, row['revision'], row['permit_id'])


def _verify_snapshot(db, account_id, course_id, revision):
    envelope = catalog.get(db, account_id, course_id)
    if envelope['revision'] != revision:
        raise catalog.Conflict('stale projection revision')
    snapshot = catalog.validate_snapshot(envelope['snapshot'])
    if snapshot['course_id'] != course_id:
        raise ValueError('projection course identity mismatch')


def acquire(db, account_id, course_id, revision, request_id):
    _identity(course_id, request_id)
    catalog._revision(revision)
    if revision < 1:
        raise catalog.Conflict('stale projection revision')
    with db:
        db.execute('BEGIN IMMEDIATE')
        prior = _row(db, account_id, course_id, request_id)
        if prior:
            if prior['revision'] != revision:
                raise catalog.Conflict('projection request identity changed')
            _verify_active(db, account_id, course_id, prior)
            _verify_snapshot(db, account_id, course_id, revision)
            return _result(prior)
        _verify_snapshot(db, account_id, course_id, revision)
        permit_id = secrets.token_hex(32)
        changed = db.execute('UPDATE course_catalog SET permit_id=? WHERE account_id=? AND course_id=? AND revision=? AND permit_id IS NULL',
            (permit_id, account_id, course_id, revision))
        if changed.rowcount != 1:
            raise catalog.Conflict('another shared projection is running')
        db.execute('INSERT INTO catalog_projection_receipts VALUES(?,?,?,?,?,?,?,NULL)',
            (account_id, course_id, request_id, revision, permit_id, 'acquired', time.time()))
        return _result(_row(db, account_id, course_id, request_id))


def read(db, account_id, course_id, request_id):
    _identity(course_id, request_id)
    # A transaction gives the receipt and fencing row a consistent read.
    with db:
        db.execute('BEGIN')
        row = _row(db, account_id, course_id, request_id)
        if row is None:
            # Unknown does not prove a queued acquire cannot still commit.
            raise catalog.Conflict('projection request not yet known')
        if row['state'] == 'acquired':
            _verify_active(db, account_id, course_id, row)
            _verify_snapshot(db, account_id, course_id, row['revision'])
        elif row['state'] != 'released':
            raise catalog.Conflict('invalid projection receipt state')
        return _result(row)


def release(db, account_id, course_id, request_id, permit_id):
    _identity(course_id, request_id)
    if not isinstance(permit_id, str) or not permit_id:
        raise ValueError('invalid permit')
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, account_id, course_id, request_id)
        if row is None or row['permit_id'] != permit_id:
            raise catalog.Conflict('projection permit mismatch')
        if row['state'] == 'released':
            # Returning the original receipt cannot clear a newer writer.
            return {'released': True}
        _verify_active(db, account_id, course_id, row)
        changed = db.execute('UPDATE course_catalog SET permit_id=NULL WHERE account_id=? AND course_id=? AND revision=? AND permit_id=?',
            (account_id, course_id, row['revision'], permit_id))
        if changed.rowcount != 1:
            raise catalog.Conflict('projection permit mismatch')
        db.execute("UPDATE catalog_projection_receipts SET state='released',released_at=? WHERE account_id=? AND course_id=? AND client_request_id=?",
            (time.time(), account_id, course_id, request_id))
        return {'released': True}
