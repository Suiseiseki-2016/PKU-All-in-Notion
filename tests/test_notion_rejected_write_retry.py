"""Definitive throttling can retry; ambiguous appends must not replay."""
import httpx
import pytest

from test_notion_shared_request_cooldown import coherent_retry_clock as coherent_retry_clock

from pku_sync import notion


@pytest.mark.parametrize('status,code', [(429, 'rate_limited'), (529, 'service_overload')])
def test_explicit_throttle_retries_append_even_when_ambiguous_retry_disabled(monkeypatch, status, code):
    calls, sleeps = [], []
    monkeypatch.setattr(notion.time, 'sleep', sleeps.append)

    def handler(request):
        calls.append(request.content)
        if len(calls) == 1:
            return httpx.Response(status, headers={'Retry-After': '61'}, json={'code': code})
        return httpx.Response(200, json={'results': [{'id': 'b' * 32}]})

    with notion.NotionClient('test', transport=httpx.MockTransport(handler)) as client:
        rows = client.append_blocks('a' * 32, notion.markdown_to_blocks('source'), retry=False)
    assert rows == [{'id': 'b' * 32}]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert sleeps == [61.0]


@pytest.mark.parametrize('status', [500, 502, 503, 504])
def test_ambiguous_server_failure_never_replays_protected_append(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'code': 'service_unavailable'})

    with notion.NotionClient('test', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(notion.NotionError):
            client.append_blocks('a' * 32, notion.markdown_to_blocks('source'), retry=False)
    assert len(calls) == 1


def test_api_access_blocked_is_not_retried(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(notion.time, 'sleep', sleeps.append)

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={'code': 'rate_limited',
            'additional_data': {'rate_limit_reason': 'public_api_request_blocked'}})

    with notion.NotionClient('test', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(notion.NotionError):
            client.whoami()
    assert len(calls) == 1 and not sleeps


def test_protected_append_throttling_remains_bounded(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(notion.time, 'sleep', sleeps.append)

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': '5'}, json={'code': 'rate_limited'})

    with notion.NotionClient('test', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(notion.NotionError) as exc:
            client.append_blocks('a' * 32, notion.markdown_to_blocks('source'), retry=False)
    assert exc.value.status == 429
    assert len(calls) == 3 and sleeps == [5.0, 5.0]
