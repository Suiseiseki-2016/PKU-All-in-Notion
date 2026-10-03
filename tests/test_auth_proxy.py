"""Campus login honors only the desktop's explicit identity-proxy override."""
from __future__ import annotations

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
