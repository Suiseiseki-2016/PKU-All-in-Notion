"""Panel auth API + fake bridge state machine (no live mail)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from pku_sync.panel.platform_bridge import FakePlatformBridge
from pku_sync.panel.webapi import create_app


class _NoopRunner:
    def submit(self, kind):
        return None

    def current(self):
        return None

    def last(self):
        return None


def make_client(platform: FakePlatformBridge) -> TestClient:
    return TestClient(
        create_app(
            settings=SimpleNamespace(data_dir="data", platform_token=""),
            runner=_NoopRunner(),
            platform_service=platform,
        )
    )


def test_panel_register_login_logout_state_machine():
    bridge = FakePlatformBridge(activated=False)
    client = make_client(bridge)

    registered = client.post(
        "/api/auth/register",
        json={"email": "New@Example.com", "password": "password1"},
    )
    assert registered.status_code == 200
    body = registered.json()
    assert body["email"] == "new@example.com"
    assert body["email_verified"] is False
    assert body["active"] is True

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email_verified"] is False

    blocked = client.post("/api/platform/redeem", json={"code": "DEADBEEF"})
    assert blocked.status_code == 403

    bridge.mark_verified()
    redeemed = client.post("/api/platform/redeem", json={"code": "DEADBEEF"})
    assert redeemed.status_code == 200
    assert redeemed.json()["transcribe_seconds_remaining"] == 3600

    logged_out = client.post("/api/auth/logout")
    assert logged_out.status_code == 200
    assert client.get("/api/auth/me").json() == {"active": False}

    login = client.post(
        "/api/auth/login",
        json={"email": "new@example.com", "password": "password1"},
    )
    assert login.status_code == 200
    assert login.json()["email_verified"] is True


def test_panel_forgot_and_resend_do_not_require_session():
    bridge = FakePlatformBridge(activated=False)
    client = make_client(bridge)

    forgot = client.post("/api/auth/forgot", json={"email": "a@b.c"})
    assert forgot.status_code == 200
    assert forgot.json() == {"ok": True}

    resend = client.post(
        "/api/auth/resend-verification", json={"email": "a@b.c"}
    )
    assert resend.status_code == 200
    assert {"op": "forgot", "email": "a@b.c"} in bridge.auth_calls
    assert {"op": "resend", "email": "a@b.c"} in bridge.auth_calls


def test_panel_quota_exposes_email_verified_without_token():
    bridge = FakePlatformBridge(
        activated=True, email="demo@example.com", email_verified=False
    )
    client = make_client(bridge)
    quota = client.get("/api/platform/quota")
    assert quota.status_code == 200
    payload = quota.json()
    assert payload["email"] == "demo@example.com"
    assert payload["email_verified"] is False
    assert "platform_token" not in payload
    assert "token" not in payload
