"""Transport loss must not erase the last confirmed course read state."""
from types import SimpleNamespace
import httpx
import pytest

from pku_sync import auth, catalog_snapshots as catalog, discover, materials, platform
from pku_sync.models import Course
from pku_sync.panel.recordings_api import sync_recording_index


@pytest.mark.parametrize('failure', [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_exception_is_distinct_from_per_course_rejection(tmp_path, monkeypatch, failure):
    settings = SimpleNamespace(data_dir=tmp_path, platform_token='developer-fixture', cloud_transcribe_url='https://fixture.invalid/v1/transcribe')
    def lose_request(*args, **kwargs):
        raise failure('private credential and path must not be exposed')
    monkeypatch.setattr(platform, '_post', lose_request)
    with pytest.raises(catalog.CatalogUnavailable) as caught:
        catalog.begin_observation(settings, '_123_1')
    assert type(caught.value).__name__ == 'CatalogTransportUnavailable'
    assert isinstance(caught.value.__cause__, failure)
    assert 'private' not in str(caught.value) and '重试' in str(caught.value)


def test_global_transport_loss_stops_before_invalidating_confirmed_cache(tmp_path, monkeypatch):
    original = b'{"complete":true,"reason":"complete","courses":{"_123_1":{"complete":true,"reason":"complete"}}}'
    ledger = tmp_path / 'portal-read-status.json'
    ledger.write_bytes(original)
    course = Course(course_id='_123_1', name='Developer confirmed course')
    course_dir = tmp_path / course.directory
    course_dir.mkdir()
    meta = course_dir / 'course.json'
    meta.write_bytes(b'previous verified metadata')
    calls = []
    client = SimpleNamespace(close=lambda: calls.append('closed'))
    settings = SimpleNamespace(data_dir=tmp_path, pku_username='developer', pku_password='developer',
        platform_token='developer-fixture', cloud_transcribe_url='https://fixture.invalid/v1/transcribe')
    monkeypatch.setattr(catalog, 'activate_account', lambda s: None)
    monkeypatch.setattr(auth, 'get_session', lambda **kwargs: client)
    monkeypatch.setattr(discover, 'discover_courses', lambda c: [course])
    monkeypatch.setattr(discover, 'select_courses', lambda c, s: c)
    def disconnected(*args, **kwargs):
        calls.append('observation')
        raise httpx.ConnectError('developer transport fixture')
    monkeypatch.setattr(platform, '_post', disconnected)
    monkeypatch.setattr(materials, 'fetch_deadlines', lambda *a: pytest.fail('transport loss must stop before additional portal work'))
    with pytest.raises(catalog.CatalogUnavailable):
        sync_recording_index(settings, lambda stage: None)
    assert calls == ['observation', 'closed']
    assert ledger.read_bytes() == original and meta.read_bytes() == b'previous verified metadata'


def test_portal_login_transport_loss_does_not_erase_confirmed_cache(tmp_path, monkeypatch):
    ledger = tmp_path / 'portal-read-status.json'
    original = b'{"complete":true,"reason":"complete"}'
    ledger.write_bytes(original)
    settings = SimpleNamespace(data_dir=tmp_path, pku_username='developer', pku_password='developer')
    monkeypatch.setattr(catalog, 'activate_account', lambda s: None)
    def disconnected(**kwargs):
        raise httpx.ConnectError('developer portal transport fixture')
    monkeypatch.setattr(auth, 'get_session', disconnected)
    with pytest.raises(httpx.ConnectError):
        sync_recording_index(settings, lambda stage: None)
    assert ledger.read_bytes() == original
