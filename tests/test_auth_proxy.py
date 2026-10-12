"""Campus login honors only the desktop's explicit identity-proxy override."""
from __future__ import annotations

import httpx
import pytest

from pku_sync import auth, network


class _Socket:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_campus_proxy_skips_dead_env_and_uses_desktop_user_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    for name in ("https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PKU_CAMPUS_IDENTITY_PROXY", "127.0.0.1:7897")
    def connect(address, timeout):
        if address[1] == 7890:
            raise OSError("closed")
        assert address == ("127.0.0.1", 7897)
        return _Socket()
    monkeypatch.setattr(network.socket, "create_connection", connect)
    assert network.campus_identity_proxy() == "http://127.0.0.1:7897"


def test_desktop_direct_choice_overrides_generic_proxy_environment(monkeypatch):
    monkeypatch.setenv("PKU_CAMPUS_IDENTITY_PROXY", "direct")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    assert network.campus_identity_proxy() is None


def test_campus_client_mounts_proxy_only_for_identity_host(monkeypatch):
    monkeypatch.setattr(auth, "campus_identity_proxy", lambda: "http://127.0.0.1:7897")
    seen = {}
    def transport(**kwargs):
        return kwargs
    def client(**kwargs):
        seen.update(kwargs)
        return seen
    monkeypatch.setattr(auth.httpx, "HTTPTransport", transport)
    monkeypatch.setattr(auth.httpx, "Client", client)
    assert auth.new_client() is seen
    assert seen["mounts"][auth.IAAA_BASE]["proxy"] == "http://127.0.0.1:7897"
    assert "proxy" not in seen["transport"]

def test_failed_campus_login_closes_its_client(monkeypatch):
    class Client:
        closed = False
        def close(self): self.closed = True
    client = Client()
    monkeypatch.setattr(auth, "new_client", lambda: client)
    def fail(*args):
        raise RuntimeError("identity host unavailable")
    monkeypatch.setattr(auth, "iaaa_authenticate", fail)
    import pytest
    with pytest.raises(RuntimeError, match="identity host unavailable"):
        auth._login("student", "secret")
    assert client.closed


def test_shell_proxy_environment_is_never_forced_onto_identity_host(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:7897")
    monkeypatch.delenv("PKU_CAMPUS_IDENTITY_PROXY", raising=False)
    def connect(address, timeout):
        raise AssertionError("no proxy may be probed or inherited from the shell env")
    monkeypatch.setattr(network.socket, "create_connection", connect)
    assert network.campus_identity_proxy() is None


def test_dead_desktop_proxy_override_falls_back_to_direct(monkeypatch):
    monkeypatch.setenv("PKU_CAMPUS_IDENTITY_PROXY", "127.0.0.1:7890")
    def connect(address, timeout):
        raise OSError("closed")
    monkeypatch.setattr(network.socket, "create_connection", connect)
    assert network.campus_identity_proxy() is None


def test_login_transport_failure_exhausts_proxy_then_uses_direct(monkeypatch):
    calls = []

    def login(username, password, *, force_direct=False):
        calls.append(force_direct)
        if not force_direct:
            request = httpx.Request("GET", auth.IAAA_BASE)
            raise httpx.ReadTimeout("proxy timed out", request=request)
        return "direct-session"

    monkeypatch.setattr(auth, "_login", login)
    monkeypatch.setattr(auth, "campus_identity_proxy", lambda: "http://127.0.0.1:7897")
    monkeypatch.setattr(auth.time, "sleep", lambda _seconds: None)
    assert auth.get_session(attempts=2, username="student", password="secret") == "direct-session"
    assert calls == [False, False, True]


def test_direct_identity_failure_recovers_via_windows_proxy_without_proxying_course_host(monkeypatch):
    calls = []
    def login(username, password, *, force_direct=False, identity_proxy=None):
        calls.append((force_direct, identity_proxy))
        if identity_proxy:
            return 'recovered-session'
        raise httpx.ConnectTimeout('unreachable direct identity host')
    monkeypatch.setattr(auth, '_login', login)
    monkeypatch.setattr(auth, 'campus_identity_proxy', lambda: None)
    monkeypatch.setattr(auth, 'campus_identity_fallback_proxy', lambda: 'http://127.0.0.1:7897')
    assert auth.get_session(username='student', password='secret') == 'recovered-session'
    assert calls == [(False, None), (False, 'http://127.0.0.1:7897')]
    seen = {}
    monkeypatch.setattr(auth.httpx, 'HTTPTransport', lambda **kwargs: kwargs)
    monkeypatch.setattr(auth.httpx, 'Client', lambda **kwargs: seen.update(kwargs) or seen)
    auth.new_client(identity_proxy='http://127.0.0.1:7897')
    assert set(seen['mounts']) == {auth.IAAA_BASE}
    assert 'proxy' not in seen['transport']


def test_bad_credentials_never_try_network_recovery(monkeypatch):
    calls = []
    def login(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('rejected credentials')
    monkeypatch.setattr(auth, '_login', login)
    monkeypatch.setattr(auth, 'campus_identity_proxy', lambda: None)
    monkeypatch.setattr(auth, 'campus_identity_fallback_proxy', lambda: 'http://127.0.0.1:7897')
    import pytest
    with pytest.raises(RuntimeError, match='rejected credentials'):
        auth.get_session(username='student', password='wrong')
    assert calls == [{}]


def test_direct_and_windows_recovery_remain_bounded_on_real_transport_failure(monkeypatch):
    calls = []
    def login(*args, **kwargs):
        calls.append(kwargs.get('identity_proxy'))
        raise httpx.ConnectTimeout('unreachable')
    monkeypatch.setattr(auth, '_login', login)
    monkeypatch.setattr(auth, 'campus_identity_proxy', lambda: None)
    monkeypatch.setattr(auth, 'campus_identity_fallback_proxy', lambda: 'http://127.0.0.1:7897')
    monkeypatch.setattr(auth.time, 'sleep', lambda _: None)
    import pytest
    with pytest.raises(RuntimeError, match='after 2 attempts'):
        auth.get_session(username='student', password='secret')
    assert calls == [None, 'http://127.0.0.1:7897', None]


def test_windows_recovery_keeps_explicit_direct_preference(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(network, 'os', SimpleNamespace(name='nt', environ={'PKU_CAMPUS_IDENTITY_PROXY':'direct'}))
    assert network.campus_identity_fallback_proxy() is None


@pytest.mark.parametrize('server,enabled,alive,expected', [
    ('127.0.0.1:7897', 1, True, 'http://127.0.0.1:7897'),
    ('http=127.0.0.1:7890;https=127.0.0.1:7897', 1, True, 'http://127.0.0.1:7897'),
    ('http=127.0.0.1:7890', 1, True, None),
    ('127.0.0.1:7897', 0, True, None),
    ('127.0.0.1:7897', 1, False, None),
    ('proxy.example:7897', 1, True, None),
    ('user:password@127.0.0.1:7897', 1, True, None),
    ('127.0.0.1:99999', 1, True, None),
])
def test_windows_recovery_requires_enabled_live_local_user_proxy(monkeypatch, server, enabled, alive, expected):
    import sys
    from types import SimpleNamespace
    values = {'ProxyEnable': enabled, 'ProxyServer': server}
    fake_registry = SimpleNamespace(HKEY_CURRENT_USER=0, OpenKey=lambda *args: _Socket(),
                                    QueryValueEx=lambda key, name: (values[name], 0))
    monkeypatch.setitem(sys.modules, 'winreg', fake_registry)
    monkeypatch.setattr(network, 'os', SimpleNamespace(name='nt', environ={}))
    def connect(address, timeout):
        assert address == ('127.0.0.1', 7897) and timeout == 0.2
        if not alive:
            raise OSError('closed')
        return _Socket()
    monkeypatch.setattr(network.socket, 'create_connection', connect)
    assert network.campus_identity_fallback_proxy() == expected
