"""Real loopback callback handling; developer checks, not native E2E."""
from contextlib import contextmanager
import threading

import httpx
import pytest

from pku_sync.notion_login import _CallbackServer


@contextmanager
def callback_listener():
    server = _CallbackServer(0, 'current-state')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',
                          trust_env=False, timeout=5) as client:
            yield server, client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize('params', [
    {'error': 'access_denied', 'state': 'foreign-state'},
    {'error': 'access_denied'},
    [('error', 'access_denied'), ('state', 'current-state'), ('state', 'foreign-state')],
])
def test_untrusted_denial_cannot_end_current_authorization(params):
    with callback_listener() as (server, client):
        rejected = client.get('/callback', params=params)
        assert rejected.status_code == 400
        assert server.result == {}
        accepted = client.get('/callback', params={'error': 'access_denied', 'state': 'current-state'})
        assert accepted.status_code == 400
        assert server.result == {'error': 'access_denied'}


def test_callback_error_never_inserts_provider_text_as_html():
    with callback_listener() as (server, client):
        untrusted = '<img src=x onerror=alert(1)>'
        response = client.get('/callback', params={'state': 'current-state', 'error': untrusted})
        assert response.status_code == 400
        assert untrusted not in response.text
        assert server.result == {'error': 'provider_error'}


def test_first_matching_callback_is_not_overwritten_by_later_tab():
    with callback_listener() as (server, client):
        first = client.get('/callback', params={'state': 'current-state', 'code': 'original-code'})
        assert first.status_code == 200
        later = client.get('/callback', params={'state': 'current-state', 'error': 'access_denied'})
        assert later.status_code == 409
        assert server.result == {'code': 'original-code'}
