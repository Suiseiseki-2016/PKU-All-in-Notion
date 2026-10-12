"""Cancelling before metadata work must preserve the confirmed course ledger."""
import threading
from types import SimpleNamespace

import pytest
from pku_sync import auth, catalog_snapshots as catalog, discover, materials
from pku_sync.models import Course
from pku_sync.panel.recordings_api import sync_recording_index


@pytest.mark.parametrize('cancel_at', ['observation', 'fresh-list'])
def test_cancel_during_preflight_stops_before_confirmed_state_or_calendar_changes(tmp_path, monkeypatch, cancel_at):
    ledger = tmp_path / 'portal-read-status.json'
    original = b'{"complete":true,"reason":"complete","courses":{"_123_1":{"complete":true}}}'
    ledger.write_bytes(original)
    course = Course(course_id='_123_1', name='Developer confirmed course')
    directory = tmp_path / course.directory
    directory.mkdir()
    meta = directory / 'course.json'
    meta.write_bytes(b'previous confirmed course metadata')
    cancelled = threading.Event()
    calls = []
    client = SimpleNamespace(close=lambda: calls.append('closed'))
    settings = SimpleNamespace(data_dir=tmp_path, pku_username='developer', pku_password='developer')
    monkeypatch.setattr(catalog, 'activate_account', lambda s: None)
    monkeypatch.setattr(auth, 'get_session', lambda **kwargs: client)
    def courses(client):
        calls.append('discover')
        if cancel_at == 'fresh-list' and calls.count('discover') == 2:
            cancelled.set()
        return [course]
    monkeypatch.setattr(discover, 'discover_courses', courses)
    monkeypatch.setattr(discover, 'select_courses', lambda courses, settings: courses)
    def observed(*args):
        calls.append('observation')
        if cancel_at == 'observation':
            cancelled.set()
        return {'revision': 1, 'observation_id': 'developer-observation'}
    monkeypatch.setattr(catalog, 'begin_observation', observed)
    monkeypatch.setattr(materials, 'fetch_deadlines', lambda *args: pytest.fail('Cancelled preflight must not continue portal work'))
    def stage(value):
        if cancelled.is_set():
            raise ValueError('目录更新已取消。')
    with pytest.raises(ValueError, match='目录更新已取消'):
        sync_recording_index(settings, stage)
    assert calls[-1] == 'closed'
    assert ledger.read_bytes() == original
    assert meta.read_bytes() == b'previous confirmed course metadata'
