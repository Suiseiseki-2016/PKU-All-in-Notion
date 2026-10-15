"""Developer protocol checks; these never count as native E2E evidence."""
from contextlib import nullcontext
import hashlib
import errno
from types import SimpleNamespace

import httpx
import pytest

from product_e2e.fault_boundary import AccountFaultBoundary
from product_e2e.core import ResourceBlocked


@pytest.mark.parametrize('variant', ['dns', 'proxy'])
def test_transport_failure_uses_original_method_path_and_payload(monkeypatch, variant):
    import socket
    owner = SimpleNamespace(config={}, actions=[], network=[])
    boundary = AccountFaultBoundary(owner)
    sent = []
    class Upstream:
        def request(self, method, url, **kwargs):
            sent.append((method, url, kwargs['content']))
            cause = socket.gaierror(11001, 'developer DNS fixture') if variant == 'dns' else ConnectionRefusedError(10061, 'developer proxy fixture')
            raise httpx.ConnectError('developer transport fixture') from cause
    monkeypatch.setattr('product_e2e.fault_boundary.httpx.Client', lambda **kwargs: nullcontext(Upstream()))
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: (_ for _ in ()).throw(socket.gaierror(11001, 'developer preflight')))
    payload = b'original request content'
    try:
        if variant == 'dns':
            # A mocked client error or standalone DNS preflight cannot prove
            # the actual delegated request used the owned DNS resolver.
            with pytest.raises(ResourceBlocked, match='dns_failure_not_observed'):
                boundary._network_failure(variant, 'POST', '/v1/catalog/_123_1/observations', payload, {})
        else:
            proof = boundary._network_failure(variant, 'POST', '/v1/catalog/_123_1/observations', payload, {})
            assert proof['request_body_sha256'] == hashlib.sha256(payload).hexdigest()
            assert proof['method'] == 'POST' and proof['path'] == '/v1/catalog/_123_1/observations'
            assert proof['failure_observed'] is True and proof['error_chain']
        assert sent == [('POST', 'https://pku.aeoluswu.info/v1/catalog/_123_1/observations', payload)]
    finally:
        boundary.close()


def test_unexpected_success_cannot_be_reported_as_proxy_failure(monkeypatch):
    owner = SimpleNamespace(config={}, actions=[], network=[])
    boundary = AccountFaultBoundary(owner)
    class Upstream:
        def request(self, *args, **kwargs):
            return httpx.Response(200)
    monkeypatch.setattr('product_e2e.fault_boundary.httpx.Client', lambda **kwargs: nullcontext(Upstream()))
    try:
        from product_e2e.core import ResourceBlocked
        with pytest.raises(ResourceBlocked, match='proxy_failure_not_observed'):
            boundary._network_failure('proxy', 'GET', '/v1/catalog/_123_1', None, {})
    finally:
        boundary.close()


def test_network_scope_is_exact_and_does_not_expire_login_or_mutate_other_courses():
    owner = SimpleNamespace(config={'network_fault_course_id': '_123_1'}, actions=[], network=[])
    boundary = AccountFaultBoundary(owner)
    try:
        for variant in ['offline', 'dns', 'proxy', 'timeout']:
            assert boundary._targeted('/v1/catalog/_123_1', 'GET', variant)
            for method, path in [('GET', '/v1/auth/me'), ('GET', '/v1/catalog/_456_1'), ('POST', '/v1/catalog/_123_1/projection')]:
                assert not boundary._targeted(path, method, variant)
    finally:
        boundary.close()


def test_actual_owned_nonlistening_proxy_has_system_refusal():
    boundary = AccountFaultBoundary(SimpleNamespace(config={}, actions=[], network=[]))
    observed = []
    error_chain = boundary._error_chain
    def capture(exc):
        value = error_chain(exc)
        observed.append(value)
        return value
    boundary._error_chain = capture
    try:
        try:
            proof = boundary._network_failure('proxy', 'GET', '/v1/catalog/_123_1', None, {})
        except ResourceBlocked:
            pytest.fail('Actual proxy exception types and OS codes: ' + repr(observed))
        assert proof['failure_observed'] is True
        assert any(row['errno'] in {10061, 111, errno.ECONNREFUSED} or row['winerror'] == 10061 for row in proof['error_chain'])
    finally:
        boundary.close()


@pytest.mark.parametrize('native_errno,observed_errno,passed', [
    (61, 61, True), (111, 111, True), (10061, 10061, True),
    (61, 13, False), (111, 61, False),
])
def test_proxy_requires_platform_connection_refusal(monkeypatch, native_errno, observed_errno, passed):
    from product_e2e import fault_boundary
    monkeypatch.setattr(fault_boundary.errno, 'ECONNREFUSED', native_errno)
    class Upstream:
        def request(self, *args, **kwargs):
            raise httpx.ConnectError('developer transport fixture') from OSError(observed_errno, 'developer refusal fixture')
    monkeypatch.setattr(fault_boundary.httpx, 'Client', lambda **kwargs: nullcontext(Upstream()))
    boundary = AccountFaultBoundary(SimpleNamespace(config={}, actions=[], network=[]))
    try:
        if passed:
            proof = boundary._network_failure('proxy', 'GET', '/v1/catalog/_123_1', None, {})
            assert proof['failure_observed'] is True
        else:
            with pytest.raises(ResourceBlocked, match='proxy_failure_not_observed'):
                boundary._network_failure('proxy', 'GET', '/v1/catalog/_123_1', None, {})
    finally:
        boundary.close()


def test_timed_out_native_peer_retains_its_own_completed_receipt(monkeypatch):
    import time
    from product_e2e import fault_boundary
    boundary = AccountFaultBoundary(SimpleNamespace(config={}, actions=[], network=[]))
    real_sleep = time.sleep
    monkeypatch.setattr(fault_boundary.time, 'sleep', lambda seconds: real_sleep(0.1))
    try:
        with boundary.inject('timeout') as receipt:
            with httpx.Client(trust_env=False, timeout=0.03) as client:
                with pytest.raises(httpx.ReadTimeout):
                    client.get(boundary.origin + '/v1/catalog/_123_1')
        assert receipt['observed'] is True and receipt['request_count'] == 1
        row = boundary.owner.network[-1]
        assert row['fault'] == 'timeout' and row['transport_evidence']['failure_observed'] is True
        assert not boundary._inflight
        from product_e2e.core import ResourceBlocked
        with pytest.raises(ResourceBlocked, match='fault_boundary_not_exercised_dns'):
            with boundary.inject('dns') as next_receipt:
                pass
        assert next_receipt['observed'] is False
    finally:
        boundary.close()
