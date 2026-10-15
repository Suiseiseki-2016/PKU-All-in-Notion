"""Actual loopback DNS packets; no application response is fabricated."""
from types import SimpleNamespace
import hashlib

import httpx
import pytest

from product_e2e.core import ResourceBlocked
from product_e2e.dns_fault import OwnedDNS, DNSHTTPTransport
from product_e2e.fault_boundary import AccountFaultBoundary


@pytest.mark.parametrize('method,body', [('GET', None), ('POST', b'{"official":"source"}')])
def test_delegated_request_really_fails_dns_even_if_system_invalid_names_resolve(monkeypatch, method, body):
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('System DNS must not be changed or needed'))
    owner = SimpleNamespace(network=[])
    boundary = AccountFaultBoundary(owner)
    try:
        proof = boundary._network_failure('dns', method, '/v1/catalog/_123_1', body,
                                           {'Authorization': 'Bearer synthetic-never-transmitted'})
        assert proof['failure_observed'] is True
        assert any(row['type'] == 'gaierror' for row in proof['error_chain'])
        assert len(proof['dns_queries']) == 1
        query = proof['dns_queries'][0]
        assert query['hostname'] == 'pku.aeoluswu.info' and query['rcode'] == 3 and query['answers'] == 0
        assert len(query['request_sha256']) == len(query['response_sha256']) == 64
        assert proof['delegated_requests'] == [{'method': method, 'host': 'pku.aeoluswu.info',
            'path': '/v1/catalog/_123_1', 'body_sha256': hashlib.sha256(body or b'').hexdigest()}]
        assert 'synthetic-never-transmitted' not in str(proof)
    finally:
        boundary.close()


def test_no_dns_failure_can_be_claimed_from_a_side_probe_or_configured_flag(monkeypatch):
    monkeypatch.setattr(OwnedDNS, 'fail_resolution', lambda *args: None)
    with OwnedDNS() as resolver:
        with httpx.Client(transport=DNSHTTPTransport(resolver), trust_env=False) as client:
            with pytest.raises(ResourceBlocked, match='resolution_did_not_fail'):
                client.get('https://pku.aeoluswu.info/v1/catalog/_123_1')
        assert resolver.queries == []


@pytest.mark.parametrize('url', ['https://example.com/v1/profile', 'https://pku.aeoluswu.info:444/v1/profile'])
def test_dns_fault_is_restricted_to_original_account_https_service(url):
    with OwnedDNS() as resolver:
        with httpx.Client(transport=DNSHTTPTransport(resolver), trust_env=False) as client:
            with pytest.raises(ResourceBlocked):
                client.get(url)
        assert resolver.queries == []
