"""Product email+password auth and verification landings."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from server import accounts, mail, product_auth, relay


class CaptureMail:
    def __init__(self):
        self.messages: list[dict] = []

    def send(self, *, to: str, subject: str, text: str, html: str | None = None):
        self.messages.append(
            {"to": to, "subject": subject, "text": text, "html": html or ""}
        )
        return mail.SentMail(provider="capture", to=to, subject=subject, id="cap-1")


def mail_config(**overrides) -> mail.MailConfig:
    base = dict(
        provider="log",
        api_key="",
        mail_from="noreply@mail.pku.aeoluswu.info",
        app_public_url="https://pku.aeoluswu.info",
        token_secret="test-secret",
        token_ttl_seconds=1800,
    )
    base.update(overrides)
    return mail.MailConfig(**base)


def make_app(tmp_path, *, capture: CaptureMail | None = None):
    sender = capture or CaptureMail()
    config = mail_config()
    app = relay.create_relay_app(
        tmp_path / "accounts.db",
        mail_config=config,
        mail_sender=sender,
    )
    return TestClient(app), sender, config


def _token_from_text(text: str) -> str:
    match = re.search(r"token=([^\s&]+)", text)
    assert match, text
    return match.group(1)


def test_register_login_verify_then_redeem(tmp_path):
    client, sender, config = make_app(tmp_path)
    registered = client.post(
        "/v1/auth/register",
        json={"email": "Student@Example.com", "password": "password1"},
    )
    assert registered.status_code == 200, registered.text
    body = registered.json()
    assert body["email"] == "student@example.com"
    assert body["email_verified"] is False
    token = body["platform_token"]
    assert sender.messages and "verify?token=" in sender.messages[0]["text"]

    blocked = client.post(
        "/v1/redeem",
        headers={"Authorization": f"Bearer {token}"},
        json={"code": "DEADBEEF"},
    )
    assert blocked.status_code == 403

    verify_token = _token_from_text(sender.messages[0]["text"])
    page = client.get(f"/verify?token={verify_token}")
    assert page.status_code == 200
    assert "已验证" in page.text

    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email_verified"] is True

    conn = accounts.connect(tmp_path / "accounts.db")
    code = accounts.create_code(conn, 600, llm_millipoints=5_000)
    conn.close()
    redeemed = client.post(
        "/v1/redeem",
        headers={"Authorization": f"Bearer {token}"},
        json={"code": code},
    )
    assert redeemed.status_code == 200
    assert redeemed.json()["transcribe_seconds_remaining"] == 600

    login = client.post(
        "/v1/auth/login",
        json={"email": "student@example.com", "password": "password1"},
    )
    assert login.status_code == 200
    assert login.json()["email_verified"] is True


def test_register_duplicate_email(tmp_path):
    client, _, _ = make_app(tmp_path)
    first = client.post(
        "/v1/auth/register",
        json={"email": "a@example.com", "password": "password1"},
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/auth/register",
        json={"email": "a@example.com", "password": "password1"},
    )
    assert second.status_code == 409


def test_unverified_transcribe_forbidden(tmp_path):
    client, _, _ = make_app(tmp_path)
    app = relay.create_relay_app(
        tmp_path / "accounts.db",
        asr_fn=lambda audio: {
            "language": "zh",
            "duration_seconds": 1,
            "segments": [{"start": 0.0, "text": "x"}],
        },
        mail_config=mail_config(),
        mail_sender=CaptureMail(),
    )
    client = TestClient(app)
    registered = client.post(
        "/v1/auth/register",
        json={"email": "b@example.com", "password": "password1"},
    ).json()
    response = client.post(
        "/v1/transcribe",
        headers={"Authorization": f"Bearer {registered['platform_token']}"},
        files={"audio": ("a.m4a", b"fake", "audio/mp4")},
    )
    assert response.status_code == 403


def test_legacy_activate_account_still_transcribes(tmp_path):
    conn = accounts.connect(tmp_path / "accounts.db")
    account_id = accounts.create_account(conn, "同学A")
    conn.execute(
        "UPDATE accounts SET transcribe_seconds_remaining = 600 WHERE id = ?",
        (account_id,),
    )
    conn.commit()
    token = accounts.issue_session(conn, account_id)
    conn.close()
    app = relay.create_relay_app(
        tmp_path / "accounts.db",
        asr_fn=lambda audio: {
            "language": "zh",
            "duration_seconds": 10,
            "segments": [{"start": 0.0, "text": "ok"}],
        },
    )
    client = TestClient(app)
    response = client.post(
        "/v1/transcribe",
        headers={"Authorization": f"Bearer {token}"},
        files={"audio": ("a.m4a", b"fake", "audio/mp4")},
    )
    assert response.status_code == 200


def test_password_reset_flow(tmp_path):
    client, sender, config = make_app(tmp_path)
    client.post(
        "/v1/auth/register",
        json={"email": "reset@example.com", "password": "password1"},
    )
    sender.messages.clear()
    forgot = client.post("/v1/auth/forgot", json={"email": "reset@example.com"})
    assert forgot.status_code == 200
    assert sender.messages and "reset?token=" in sender.messages[0]["text"]
    reset_token = _token_from_text(sender.messages[0]["text"])
    form = client.get(f"/reset?token={reset_token}")
    assert form.status_code == 200
    assert "新密码" in form.text
    done = client.post(
        "/reset",
        data={
            "token": reset_token,
            "password": "password2",
            "password2": "password2",
        },
    )
    assert done.status_code == 200
    assert "密码已更新" in done.text
    bad = client.post(
        "/v1/auth/login",
        json={"email": "reset@example.com", "password": "password1"},
    )
    assert bad.status_code == 401
    good = client.post(
        "/v1/auth/login",
        json={"email": "reset@example.com", "password": "password2"},
    )
    assert good.status_code == 200


def test_logout(tmp_path):
    client, _, _ = make_app(tmp_path)
    token = client.post(
        "/v1/auth/register",
        json={"email": "out@example.com", "password": "password1"},
    ).json()["platform_token"]
    assert (
        client.post(
            "/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 401
    )


def test_product_auth_helpers_reject_short_password():
    try:
        product_auth.hash_password("short")
        assert False, "expected AuthError"
    except product_auth.AuthError:
        pass
