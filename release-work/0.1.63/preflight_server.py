"""Validate the candidate against a private copy of the production database."""
import json
import os
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
from server import accounts, relay

root = Path('/candidate')
scratch = root / 'migration-check.db'
assert not scratch.exists()
with sqlite3.connect('file:/production/accounts.db?mode=ro', uri=True) as source:
    before_count = source.execute('SELECT count(*) FROM accounts').fetchone()[0]
    before_balances = source.execute(
        'SELECT sum(transcribe_seconds_remaining), sum(llm_millipoints_remaining) FROM accounts'
    ).fetchone()
    assert source.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    with sqlite3.connect(scratch) as target:
        source.backup(target)
os.chmod(scratch, 0o600)
try:
    with accounts.connect(scratch) as migrated:
        assert migrated.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert migrated.execute('SELECT count(*) FROM accounts').fetchone()[0] == before_count
        assert tuple(migrated.execute(
            'SELECT sum(transcribe_seconds_remaining), sum(llm_millipoints_remaining) FROM accounts'
        ).fetchone()) == before_balances
        columns = {row['name'] for row in migrated.execute('PRAGMA table_info(accounts)')}
        assert {'tier', 'tier_expires_at', 'basic_seconds_remaining', 'advanced_seconds_remaining',
                'invite_code', 'campus_id_hash', 'weekly_rollover_seconds'} <= columns
    # Build the app without real vendor adapters or a mail sender. Only GETs.
    app = relay.create_relay_app(scratch)
    with TestClient(app) as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/v1/quota').status_code == 401
        assert client.get('/invite/PKU-RELEASECHECK').status_code in (200, 404)
        assert client.get('/gift/GIFT-RELEASECHECK').status_code in (200, 404)
    print(json.dumps({'migration': 'passed', 'integrity': 'ok', 'account_count_unchanged': True,
                      'legacy_balances_unchanged': True, 'production_db_read_only': True,
                      'healthz': 200, 'quota_unauthenticated': 401, 'vendor_calls': 0}))
finally:
    for path in root.glob('migration-check.db*'):
        path.unlink()
