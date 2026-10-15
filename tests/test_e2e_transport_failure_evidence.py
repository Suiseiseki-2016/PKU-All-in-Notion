"""Developer causal-evidence checks; these do not count as native coverage."""
import io
import json
from types import SimpleNamespace

import httpx
import pytest

from product_e2e.notion_fault_boundary import NotionFaultBoundary
from product_e2e.oracles import Oracle, OracleError

ROOT = '32091b6f53e180fc8ec8e650b4479f5d'
CHILD = '1' * 32


def handler(body):
    return SimpleNamespace(path='/v1/pages', command='POST',
        headers={'Authorization': 'Bearer private-test-secret',
                 'Content-Length': str(len(body))}, rfile=io.BytesIO(body))


@pytest.mark.parametrize('phase', ['mutation-ancestry', 'upstream-request', 'ownership-readback'])
def test_boundary_failure_proves_whether_real_write_response_was_received(monkeypatch, phase):
    owner = SimpleNamespace(network=[])
    boundary = NotionFaultBoundary(owner, ROOT)
    sent, writes = [], []
    body = json.dumps({'parent': {'page_id': ROOT}, 'children': [{'type': 'paragraph'}]}).encode()
    def allowed(*args):
        if phase == 'mutation-ancestry':
            raise httpx.ConnectError('private-test-secret https://example.com/?secret=x')
        return True
    def forward(*args, **kwargs):
        writes.append(1)
        if phase == 'upstream-request':
            raise httpx.ReadTimeout('private-test-secret')
        return httpx.Response(200, json={'id': CHILD})
    def remember(*args):
        raise httpx.ReadError('private-test-secret')
    monkeypatch.setattr(boundary, '_upstream', lambda headers: SimpleNamespace(request=forward))
    monkeypatch.setattr(boundary, '_mutation_allowed', allowed)
    monkeypatch.setattr(boundary, '_remember_created_blocks', remember)
    monkeypatch.setattr(boundary, '_send', lambda h, status, payload, *args: sent.append(status))
    try:
        boundary.handle(handler(body))
        assert sent == [503] and len(writes) == (0 if phase == 'mutation-ancestry' else 1)
        records = [r for r in owner.network if r.get('boundary') == 'notion-boundary-error']
        assert len(records) == 1
        record = records[0]
        assert record['stage'] == phase and record['status'] == 503
        assert record['forward_started'] == (phase != 'mutation-ancestry')
        assert record['upstream_response_received'] == (phase == 'ownership-readback')
        assert record['upstream_status'] == (200 if phase == 'ownership-readback' else None)
        assert record['elapsed_seconds'] >= 0
        assert 'private-test-secret' not in json.dumps(owner.network)
    finally:
        boundary.close()


def test_safety_read_failure_is_recorded_without_credentials_or_success():
    owner = SimpleNamespace(network=[])
    boundary = NotionFaultBoundary(owner, ROOT)
    def service(request):
        raise httpx.ConnectError('private-test-secret', request=request)
    try:
        with httpx.Client(base_url='https://api.notion.com', transport=httpx.MockTransport(service)) as client:
            with pytest.raises(httpx.ConnectError):
                boundary._safety_get(client, '/v1/pages/' + CHILD)
        assert len(owner.network) == 1
        record = owner.network[0]
        assert record['method'] == 'GET' and record['status'] is None
        assert record['error_type'] == 'ConnectError' and not record['upstream_response_received']
        assert 'upstream_body_sha256' not in record
        assert 'private-test-secret' not in json.dumps(record)
    finally:
        boundary.close()


def test_oracle_transport_failure_is_durable_and_terminal(monkeypatch, tmp_path):
    monkeypatch.setenv('E2E_EVIDENCE_SECRET', 'private-test-secret')
    config = {'credentials': {'notion_token': {'env': 'E2E_EVIDENCE_SECRET'}}}
    attempts = []
    original_client = httpx.Client
    def service(request):
        attempts.append(1)
        raise httpx.ConnectError('private-test-secret https://example.com/?secret=x', request=request)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(service)))
    oracle = Oracle(config, tmp_path)
    with pytest.raises(OracleError, match='transport_unavailable'):
        oracle.get('notion', '/v1/pages/' + CHILD)
    assert len(attempts) == 1
    records = list(tmp_path.glob('oracle-*.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text('utf-8'))
    assert record['status'] is None and record['method'] == 'GET'
    assert record['error_type'] == 'ConnectError' and not record['response_received']
    assert record['elapsed_seconds'] >= 0 and 'body_sha256' not in record
    assert 'private-test-secret' not in records[0].read_text('utf-8')
    oracle.close()


def test_independent_oracle_reuses_connections_but_reads_current_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv('E2E_EVIDENCE_SECRET', 'synthetic-first-token')
    config = {'credentials': {'notion_token': {'env': 'E2E_EVIDENCE_SECRET'}}}
    constructors, authorizations = [], []
    original_client = httpx.Client
    def service(request):
        authorizations.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': CHILD})
    def client(**kwargs):
        constructors.append(kwargs)
        return original_client(**kwargs, transport=httpx.MockTransport(service))
    monkeypatch.setattr(httpx, 'Client', client)
    oracle = Oracle(config, tmp_path)
    oracle.get('notion', '/v1/pages/' + CHILD)
    monkeypatch.setenv('E2E_EVIDENCE_SECRET', 'synthetic-second-token')
    oracle.get('notion', '/v1/pages/' + CHILD)
    assert len(constructors) == 1
    assert authorizations == ['Bearer synthetic-first-token', 'Bearer synthetic-second-token']
    assert constructors[0]['trust_env'] is False and constructors[0]['follow_redirects'] is False
    oracle.close()
    oracle.get('notion', '/v1/pages/' + CHILD)
    assert len(constructors) == 2 and len(oracle.requests) == 3
    assert 'synthetic-first-token' not in ''.join(p.read_text('utf-8') for p in tmp_path.glob('*.json'))
    oracle.close()
