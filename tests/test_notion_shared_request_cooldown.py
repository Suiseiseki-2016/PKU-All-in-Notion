"""Server cooldown is shared; this is developer-only, never native evidence."""
import threading
from types import SimpleNamespace

import httpx
import pytest

from pku_sync import notion
import _notion_shared_cooldown_checks as probe


class CoherentRetryClock(probe.Clock):
    """Old sleep observers keep their exact assertions while time advances."""

    def __init__(self):
        super().__init__(automatic=True)
        self._observer = None

    @property
    def sleep(self):
        return self._sleep

    @sleep.setter
    def sleep(self, observer):
        if getattr(observer, '__self__', None) is self and getattr(observer, '__func__', None) is type(self)._sleep:
            self._observer = None
        else:
            self._observer = observer

    def _sleep(self, seconds):
        if self._observer:
            self._observer(seconds)
        super().sleep(seconds)


@pytest.fixture(autouse=True)
def coherent_retry_clock(monkeypatch):
    clock = CoherentRetryClock()
    monkeypatch.setattr(notion, '_TOKEN_COOLDOWNS', {})
    monkeypatch.setattr(notion, 'time', clock)
    monkeypatch.setattr(notion, 'threading', SimpleNamespace(
        Condition=clock.condition_class(), Lock=threading.Lock, RLock=threading.RLock))
    monkeypatch.setattr(notion, 'dead_loopback_proxy', lambda: False)
    return clock


@pytest.mark.parametrize('status,delay', [(429, 45), (529, 45), (429, 600)])
def test_other_same_token_client_waits_full_shared_server_deadline(status, delay):
    row = probe.run_parallel(f'status-{status}-delay-{delay}', status, delay)
    assert row['shared_cooldown_predicate'] is True
    assert row['different_token_isolated_predicate'] is True
    assert row['all_owned_workers_terminated'] is True


@pytest.mark.parametrize('direct,reopen', [(False, False), (True, False), (False, True)])
def test_final_direct_and_reopened_clients_keep_the_embargo(direct, reopen):
    row = probe.run_terminal(f'terminal-{direct}-{reopen}', direct=direct, reopen=reopen)
    assert row['shared_cooldown_predicate'] is True
    assert row['original_attempt_budget_preserved'] is True


def test_later_short_throttle_cannot_shorten_shared_embargo(coherent_retry_clock):
    first = notion.NotionClient('developer-token', transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    second = notion.NotionClient('developer-token', transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    other = notion.NotionClient('different-developer-token', transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        assert first._cooldown is second._cooldown
        assert first._cooldown is not other._cooldown
        first._record_throttle(httpx.Response(429, headers={'Retry-After': '600'}), 1)
        coherent_retry_clock.advance(10)
        second._record_throttle(httpx.Response(529, headers={'Retry-After': '3'}), 1)
        second._cooldown.wait()
        assert coherent_retry_clock.monotonic() == 600
    finally:
        first.close()
        second.close()
        other.close()


@pytest.mark.parametrize('method,path', [('POST', '/v1/pages'), ('PATCH', '/v1/blocks/aa'), ('POST', '/v1/file_uploads'), ('POST', '/v1/file_uploads/aa/send')])
@pytest.mark.parametrize('status', [500, 502, 503, 504])
def test_untracked_unknown_writes_never_replay_ambiguous_5xx(method, path, status):
    calls = []
    direct = []
    with notion.NotionClient('developer-write-token', transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(status, json={'code': 'service_unavailable'})),
            direct_transport=httpx.MockTransport(lambda request: direct.append(request) or httpx.Response(200))) as client:
        with pytest.raises(notion.NotionError) as error:
            client._request_transport(method, path, json_body={'kept': 'exact'}, retry=True)
    assert error.value.status == status
    assert len(calls) == 1 and direct == []


@pytest.mark.parametrize('method,path', [('GET', '/v1/pages/aa'), ('POST', '/v1/search'), ('POST', '/v1/databases/aa/query')])
def test_explicit_read_requests_keep_bounded_5xx_retry(method, path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, json={'result': 'kept'})
    with notion.NotionClient('developer-read-token', transport=httpx.MockTransport(handler)) as client:
        result = client._request_transport(method, path)
    assert result == {'result': 'kept'} and len(calls) == 2


@pytest.mark.parametrize('header,body,expected', [
    ('600', {}, 600.0), ('61', {}, 61.0), ('0', {}, 0.0),
    (None, {'additional_data': {'retry_after': '700'}}, 700.0),
    ('NaN', {}, 2.0), ('inf', {}, 2.0), ('-1', {}, 2.0),
    ('bad', {'additional_data': {'retry_after': '65'}}, 65.0)])
def test_retry_after_is_finite_complete_with_provider_body_backup(header, body, expected):
    response = httpx.Response(429, headers={'Retry-After': header} if header is not None else {}, json=body)
    assert notion._backoff(response, 1) == expected


def test_anonymous_state_never_contains_or_logs_token(coherent_retry_clock, caplog):
    token = 'secret-developer-token-never-persist'
    with notion.NotionClient(token, transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        client._record_throttle(httpx.Response(429, headers={'Retry-After': '45'}), 1)
        state = client._cooldown
    assert all(isinstance(key, bytes) and len(key) == 32 for key in notion._TOKEN_COOLDOWNS)
    assert token not in repr(notion._TOKEN_COOLDOWNS) + repr(vars(state)) + caplog.text
