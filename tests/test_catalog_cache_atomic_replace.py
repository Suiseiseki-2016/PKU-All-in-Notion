"""A live catalog reader must not abort first-home creation on Windows."""
import json
import os
from pathlib import Path
from threading import Event, Thread
import time

import pytest

from pku_sync import catalog_snapshots as catalog
from test_account_catalog import settings, snapshot


def writer(tmp_path, kind):
    if kind == 'cache':
        before = {'revision': 1, 'snapshot': snapshot()}
        catalog.save_cache(tmp_path, before)
        path = catalog.cache_path(tmp_path, before['snapshot']['course_id'])
        return path, lambda: catalog.save_cache(tmp_path, {**before, 'revision': 2})
    if kind == 'status':
        path = tmp_path / 'portal-read-status.json'
        catalog._save_read_status(path, {'complete': False})
        return path, lambda: catalog._save_read_status(path, {'complete': True})
    config = settings(tmp_path)
    catalog.activate_account(config)
    config.platform_token = 'another-test-session'
    return tmp_path / '.account-catalog/active-account', lambda: catalog.activate_account(config)


def test_real_reader_handle_does_not_abort_course_cache_refresh(tmp_path, monkeypatch):
    path, update = writer(tmp_path, 'cache')
    reader = path.open('rb')
    before = reader.read()
    attempted = Event()
    def release_reader():
        attempted.wait(timeout=2)
        time.sleep(.03)
        reader.close()
    thread = Thread(target=release_reader)
    thread.start()
    replace = Path.replace
    conflicts = []
    def observe(source, destination):
        try:
            return replace(source, destination)
        except PermissionError as exc:
            conflicts.append(exc)
            attempted.set()
            raise
    monkeypatch.setattr(Path, 'replace', observe)
    try:
        update()
    finally:
        attempted.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert json.loads(before)['revision'] == 1
    assert json.loads(path.read_text('utf8'))['revision'] == 2
    if os.name == 'nt':
        assert conflicts and all(exc.winerror in {5, 32, 33} for exc in conflicts)
    assert not list(path.parent.glob('*.tmp'))


@pytest.mark.parametrize('kind', ['cache', 'status', 'account'])
@pytest.mark.parametrize('code', [5, 32, 33])
def test_brief_windows_sharing_conflict_retries_same_local_file(tmp_path, monkeypatch, kind, code):
    path, update = writer(tmp_path, kind)
    original = path.read_bytes()
    replace = Path.replace
    attempts, delays = [], []
    def transient(source, destination):
        attempts.append(source)
        if len(attempts) < 3:
            assert path.read_bytes() == original
            error = PermissionError('temporary reader handle')
            error.winerror = code
            raise error
        return replace(source, destination)
    monkeypatch.setattr(Path, 'replace', transient)
    monkeypatch.setattr(catalog.time, 'sleep', delays.append)
    update()
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert sum(delays) <= .25
    assert path.read_bytes() != original
    assert not list(path.parent.glob('*.tmp'))


@pytest.mark.parametrize('kind', ['cache', 'status', 'account'])
@pytest.mark.parametrize('code', [5, 87, None])
def test_persistent_or_other_denial_preserves_original_and_fails(tmp_path, monkeypatch, kind, code):
    path, update = writer(tmp_path, kind)
    original = path.read_bytes()
    attempts, delays = [], []
    def denied(source, destination):
        attempts.append(source)
        error = PermissionError('persistent denial')
        if code is not None:
            error.winerror = code
        raise error
    monkeypatch.setattr(Path, 'replace', denied)
    monkeypatch.setattr(catalog.time, 'sleep', delays.append)
    with pytest.raises(PermissionError):
        update()
    assert len(attempts) == (6 if code == 5 else 1)
    assert sum(delays) <= .25
    assert path.read_bytes() == original
    assert not list(path.parent.glob('*.tmp'))
