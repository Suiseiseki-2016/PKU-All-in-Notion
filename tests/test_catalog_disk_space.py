"""Disk errors must preserve verified metadata and name the recovery action."""
import errno
import sqlite3
import shutil
from types import SimpleNamespace

import pytest

from pku_sync.panel import recordings_api


@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('kind', ['sync', 'catalog', 'process'])
def test_disk_full_error_is_actionable_without_private_details(kind, wrapped, tmp_path):
    manager = recordings_api.RecordingWorkManager(SimpleNamespace(data_dir=tmp_path))
    manager._work = recordings_api.Work(kind=kind, stage='读取教学网目录')
    def action():
        try:
            raise OSError(errno.ENOSPC, 'private student path and token')
        except OSError as exc:
            if wrapped:
                raise ValueError('private wrapped path') from exc
            raise
    manager._run(action)
    result = manager.state()
    assert result['state'] == 'failed'
    assert '磁盘' in result['error'] and '清理' in result['error'] and '重试' in result['error']
    assert 'private' not in result['error'] and '网络' not in result['error']
    assert len(result['failure_id']) == 8


def test_sqlite_full_is_identified_by_error_code(tmp_path):
    manager = recordings_api.RecordingWorkManager(SimpleNamespace(data_dir=tmp_path))
    manager._work = recordings_api.Work(kind='sync', stage='保存目录')
    def action():
        exc = sqlite3.OperationalError('private database path')
        exc.sqlite_errorcode = sqlite3.SQLITE_FULL
        raise exc
    manager._run(action)
    assert '清理' in manager.state()['error']


def test_full_volume_refresh_preserves_confirmed_ledger_before_any_side_effect(tmp_path, monkeypatch):
    from pku_sync import catalog_snapshots, auth
    ledger = tmp_path / 'portal-read-status.json'
    original = b'{"complete":true,"reason":"complete"}'
    ledger.write_bytes(original)
    (tmp_path / 'course.json').write_bytes(b'unchanged verified course')
    settings = SimpleNamespace(data_dir=tmp_path, pku_username='test', pku_password='test')
    calls = []
    monkeypatch.setattr(shutil, 'disk_usage',
                        lambda root: SimpleNamespace(free=0))
    monkeypatch.setattr(catalog_snapshots, 'activate_account', lambda s: calls.append('activate'))
    monkeypatch.setattr(auth, 'get_session', lambda **kw: calls.append('network'))
    with pytest.raises(OSError) as caught:
        recordings_api.sync_recording_index(settings, lambda s: calls.append('stage'))
    assert caught.value.errno == errno.ENOSPC
    assert not calls
    assert ledger.read_bytes() == original
    assert (tmp_path / 'course.json').read_bytes() == b'unchanged verified course'
