"""Durable permit diagnostics; real native recovery remains a separate gate."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from pku_sync import catalog_snapshots as catalog
from test_account_catalog import settings, snapshot


def test_windows_observer_handle_cannot_break_atomic_durable_journal_replace(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes
    from pathlib import Path
    import threading
    import time
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    path = tmp_path / 'actual-windows-journal.json'
    path.write_text('{"phase":"prepared"}', 'utf8')
    # A real Windows read handle permits reads/writes but temporarily denies
    # deletion, exactly the conflict observed by the installed candidate.
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    attempted = threading.Event()
    def close_reader():
        attempted.wait(timeout=2)
        time.sleep(.03)
        kernel.CloseHandle(handle)
    reader = threading.Thread(target=close_reader)
    reader.start()
    replace, fsync = Path.replace, catalog.os.fsync
    attempts, flushes = [], []
    def observe_replace(source, target):
        attempts.append(str(source))
        try:
            return replace(source, target)
        except PermissionError as exc:
            assert exc.winerror in {5, 32, 33}
            attempted.set()
            raise
    def observe_flush(fd):
        flushes.append(fd)
        return fsync(fd)
    monkeypatch.setattr(Path, 'replace', observe_replace)
    monkeypatch.setattr(catalog.os, 'fsync', observe_flush)
    try:
        catalog._save_projection_journal(path, {'phase': 'checkpointed', 'sequence': 8})
    finally:
        attempted.set()
        reader.join(timeout=3)
    assert not reader.is_alive()
    assert len(attempts) >= 2 and len(set(attempts)) == 1
    assert len(flushes) == 1
    assert json.loads(path.read_text('utf8')) == {'phase': 'checkpointed', 'sequence': 8}


def test_persistent_windows_replace_denial_still_fails_without_overwriting_original(tmp_path, monkeypatch):
    from pathlib import Path
    path = tmp_path / 'journal.json'
    path.write_bytes(b'original receipt')
    attempts, waits = [], []
    def deny(source, target):
        attempts.append(str(source))
        error = PermissionError('Actual persistent ACL denial remains a failure')
        error.winerror = 5
        raise error
    monkeypatch.setattr(Path, 'replace', deny)
    monkeypatch.setattr(catalog.time, 'sleep', waits.append)
    with pytest.raises(PermissionError):
        catalog._save_projection_journal(path, {'phase': 'writing'})
    assert len(attempts) == 6 and len(set(attempts)) == 1
    assert sum(waits) <= .25
    assert path.read_bytes() == b'original receipt'


def setup(tmp_path, monkeypatch, fail=None):
    config = settings(tmp_path)
    envelope = {'revision': 7, 'snapshot': snapshot()}
    calls = []
    monkeypatch.setattr(catalog, 'verify_current', lambda *a: envelope)
    def call(s, method, course, suffix='', body=None):
        if method == 'GET' and course == 'projection-protocol':
            return {'revision': 0, 'snapshot': None}
        calls.append(suffix)
        if suffix == fail:
            raise catalog.CatalogUnavailable('unknown transport result')
        return {'permit_id': 'receipt', 'revision': 7} if suffix == '/projection' else {'released': True}
    monkeypatch.setattr(catalog, '_call', call)
    return config, envelope, calls


def test_receipt_persisted_before_writer_and_removed_only_after_release(tmp_path, monkeypatch):
    config, envelope, calls = setup(tmp_path, monkeypatch)
    with catalog.projection(config, envelope):
        path = catalog.projection_journal_path(config, '_101545_1')
        state = json.loads(path.read_text('utf-8'))
        assert state['permit_id'] == 'receipt' and state['phase'] == 'writing'
        assert state['revision'] == 7 and state['snapshot_sha256'] == catalog.fingerprint(envelope['snapshot'])
    assert not path.exists() and calls == ['/projection', '/projection/release']


@pytest.mark.parametrize('fail,phase', [('/projection', 'acquiring'), ('/projection/release', 'releasing')])
def test_unknown_response_retains_journal_and_restart_cannot_replay(tmp_path, monkeypatch, fail, phase):
    config, envelope, calls = setup(tmp_path, monkeypatch, fail)
    with pytest.raises(catalog.CatalogUnavailable):
        with catalog.projection(config, envelope):
            pass
    path = catalog.projection_journal_path(config, '_101545_1')
    original = path.read_bytes()
    assert json.loads(original)['phase'] == phase
    before = len(calls)
    with pytest.raises(catalog.CatalogUnavailable, match='许可回执'):
        with catalog.projection(config, envelope):
            pytest.fail('must not enter writer after unknown response')
    assert len(calls) == before and path.read_bytes() == original


def test_unreadable_prior_journal_cannot_be_discarded_or_reacquired(tmp_path, monkeypatch):
    config, envelope, calls = setup(tmp_path, monkeypatch)
    path = catalog.projection_journal_path(config, '_101545_1')
    path.parent.mkdir(parents=True)
    path.write_bytes(b'not valid json')
    with pytest.raises(catalog.CatalogUnavailable, match='许可回执'):
        with catalog.projection(config, envelope):
            pytest.fail('invalid prior journal must fail closed')
    assert not calls and path.read_bytes() == b'not valid json'


def test_disk_failure_after_acquire_releases_known_permit_without_entering_writer(tmp_path, monkeypatch):
    config, envelope, calls = setup(tmp_path, monkeypatch)
    save = catalog._save_projection_journal
    def fail_writing(path, state):
        if state['phase'] == 'writing':
            raise OSError('disk full')
        save(path, state)
    monkeypatch.setattr(catalog, '_save_projection_journal', fail_writing)
    with pytest.raises(OSError):
        with catalog.projection(config, envelope):
            pytest.fail('no Notion write without durable receipt')
    assert calls == ['/projection', '/projection/release']
    assert not catalog.projection_journal_path(config, '_101545_1').exists()


def test_unknown_legacy_notion_body_cannot_release_or_replay_its_permit(tmp_path, monkeypatch):
    config, envelope, calls = setup(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError):
        with catalog.projection(config, envelope):
            raise RuntimeError('Notion may have committed before the connection failed')
    path = catalog.projection_journal_path(config, '_101545_1')
    assert path.exists(), 'The actual unknown writer must retain its fence'
    original = path.read_bytes()
    assert json.loads(original)['phase'] == 'writing-unknown'
    assert calls == ['/projection']
    with pytest.raises(catalog.CatalogUnavailable, match='许可回执'):
        with catalog.projection(config, envelope):
            pytest.fail('An unknown legacy writer cannot be replayed')
    assert path.read_bytes() == original and calls == ['/projection']


def test_concurrent_same_profile_cannot_obtain_two_permits(tmp_path, monkeypatch):
    config, envelope, calls = setup(tmp_path, monkeypatch)
    entered, leave = Event(), Event()
    def writer():
        with catalog.projection(config, envelope):
            entered.set()
            assert leave.wait(5)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(writer)
        try:
            assert entered.wait(3)
            with pytest.raises(catalog.CatalogUnavailable, match='另一进程'):
                with catalog.projection(config, envelope):
                    pytest.fail('must not enter a concurrent writer')
            assert calls == ['/projection']
        finally:
            leave.set()
        pending.result(timeout=3)
    assert calls == ['/projection', '/projection/release']
