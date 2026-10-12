"""Read fallback cannot consume a second full caller timeout."""
import httpx
import pytest

from pku_sync import platform


def test_sync_request_scope_reuses_connection_closes_and_never_replays_writes(monkeypatch):
    clients, sent = [], []
    class ScopedClient:
        def __init__(self, **kwargs):
            self.closed = False
            clients.append(self)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.closed = True
        def request(self, method, url, **kwargs):
            sent.append((self, method, kwargs['headers']))
            if method == 'POST':
                raise httpx.ReadTimeout('A real accepted write may have lost its response')
            return httpx.Response(200, json={'revision': 3})
    monkeypatch.setattr(platform.httpx, 'Client', ScopedClient)
    monkeypatch.setattr(platform.httpx, 'get', lambda *a, **k: pytest.fail('Unpooled request'))
    with platform.request_scope():
        platform._get('https://relay.example/v1/catalog/_1', timeout=20,
                      headers={'Authorization': 'Bearer fixture-one'})
        platform._get('https://relay.example/v1/catalog/_1', timeout=20,
                      headers={'Authorization': 'Bearer fixture-one'})
        with pytest.raises(httpx.ReadTimeout):
            platform._post('https://relay.example/v1/catalog/_1/observations', timeout=20,
                           headers={'Authorization': 'Bearer fixture-one'})
        assert len(clients) == 1 and not clients[0].closed
    assert clients[0].closed and len(sent) == 3
    with platform.request_scope():
        platform._get('https://relay.example/v1/catalog/_2', timeout=20,
                      headers={'Authorization': 'Bearer fixture-two'})
    assert len(clients) == 2 and clients[1].closed
    assert sent[-1][0] is not sent[0][0]
    assert sent[-1][2] == {'Authorization': 'Bearer fixture-two'}


@pytest.mark.parametrize('spent', [20.0, 23.0])
def test_expired_read_budget_preserves_original_failure_without_another_connection(monkeypatch, spent):
    clock, sent = [100.0], []
    failure = httpx.ReadTimeout('original read timed out')
    monkeypatch.setattr(platform.time, 'monotonic', lambda: clock[0])
    def first(url, **kwargs):
        sent.append(url)
        clock[0] += spent
        raise failure
    monkeypatch.setattr(platform.httpx, 'get', first)
    monkeypatch.setattr(platform.httpx, 'Client', lambda **kwargs: pytest.fail('exhausted budget opened another connection'))
    with pytest.raises(httpx.ReadTimeout) as caught:
        platform._get('https://relay.example/v1/catalog/_106364_1', timeout=20)
    assert caught.value is failure and len(sent) == 1


@pytest.mark.parametrize('timeout', [20.0, httpx.Timeout(20, connect=8)])
def test_proxy_fallback_uses_only_remaining_budget_and_keeps_authentication(monkeypatch, timeout):
    clock, observed = [100.0], []
    monkeypatch.setattr(platform.time, 'monotonic', lambda: clock[0])
    def first(url, **kwargs):
        clock[0] += 3.0
        raise httpx.ConnectError('proxy unavailable')
    monkeypatch.setattr(platform.httpx, 'get', first)
    class Direct:
        def __init__(self, *, trust_env):
            assert trust_env is False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def request(self, method, url, **kwargs):
            observed.append(kwargs)
            assert method == 'GET' and url == 'https://relay.example/v1/catalog/_106364_1'
            return httpx.Response(200, json={'revision': 7})
    monkeypatch.setattr(platform.httpx, 'Client', Direct)
    response = platform._get('https://relay.example/v1/catalog/_106364_1', timeout=timeout,
                             headers={'Authorization': 'Bearer test-only-token'})
    assert response.json() == {'revision': 7} and len(observed) == 1
    actual_timeout = observed[0]['timeout']
    if isinstance(timeout, httpx.Timeout):
        assert actual_timeout.read == 17 and actual_timeout.connect == 8
    else:
        assert actual_timeout == 17
    assert observed[0]['headers'] == {'Authorization': 'Bearer test-only-token'}
