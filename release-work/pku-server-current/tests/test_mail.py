"""Unit tests for the transactional mail adapter."""

from __future__ import annotations

import time

import httpx
import pytest

from server import mail


def test_from_environment_none_when_unset(monkeypatch):
    monkeypatch.delenv("MAIL_PROVIDER", raising=False)
    assert mail.MailConfig.from_environment() is None


def test_from_environment_resend_requires_key(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "resend")
    monkeypatch.setenv("MAIL_FROM", "noreply@mail.pku.aeoluswu.info")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with pytest.raises(mail.MailError, match="RESEND_API_KEY"):
        mail.MailConfig.from_environment()


def test_log_sender_prints_and_returns_id(capsys):
    sender = mail.LogMailSender()
    result = sender.send(to="a@example.com", subject="hi", text="body")
    assert result.provider == "log"
    assert result.to == "a@example.com"
    assert result.id.startswith("log-")
    out = capsys.readouterr().out
    assert "a@example.com" in out
    assert "hi" in out


def test_resend_sender_posts_and_reads_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.resend.com/emails"
        assert request.headers["Authorization"] == "Bearer re_test"
        body = request.read()
        assert b"noreply@mail.pku.aeoluswu.info" in body
        assert b"student@example.com" in body
        return httpx.Response(200, json={"id": "msg_123"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        sender = mail.ResendMailSender(
            "re_test", "noreply@mail.pku.aeoluswu.info", client=client
        )
        result = sender.send(
            to="student@example.com",
            subject="验证",
            text="plain",
            html="<p>html</p>",
        )
    assert result == mail.SentMail(
        provider="resend", to="student@example.com", subject="验证", id="msg_123"
    )


def test_resend_sender_maps_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "domain not verified"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        sender = mail.ResendMailSender("re_test", "noreply@x.test", client=client)
        with pytest.raises(mail.MailError, match="HTTP 403"):
            sender.send(to="a@b.c", subject="s", text="t")


def test_email_token_round_trip():
    config = mail.MailConfig(
        provider="log",
        api_key="",
        mail_from="noreply@mail.pku.aeoluswu.info",
        app_public_url="https://pku.aeoluswu.info",
        token_secret="secret",
        token_ttl_seconds=600,
    )
    now = 1_700_000_000.0
    token = mail.issue_email_token(
        config, purpose="verify", email="Student@Example.COM", now=now
    )
    email = mail.verify_email_token(config, token, purpose="verify", now=now + 10)
    assert email == "student@example.com"


def test_email_token_rejects_expired_and_wrong_purpose():
    config = mail.MailConfig(
        provider="log",
        api_key="",
        mail_from="noreply@x.test",
        app_public_url="https://pku.aeoluswu.info",
        token_secret="secret",
        token_ttl_seconds=60,
    )
    now = time.time()
    token = mail.issue_email_token(config, purpose="verify", email="a@b.c", now=now)
    with pytest.raises(mail.MailError):
        mail.verify_email_token(config, token, purpose="reset", now=now)
    with pytest.raises(mail.MailError):
        mail.verify_email_token(config, token, purpose="verify", now=now + 120)


def test_send_verification_email_uses_app_public_url(capsys):
    config = mail.MailConfig(
        provider="log",
        api_key="",
        mail_from="noreply@mail.pku.aeoluswu.info",
        app_public_url="https://pku.aeoluswu.info",
        token_secret="secret",
    )
    sender = mail.make_sender(config)
    result = mail.send_verification_email(sender, config, to="you@example.com")
    assert result.provider == "log"
    out = capsys.readouterr().out
    assert "https://pku.aeoluswu.info/verify?token=" in out
    assert "you@example.com" in out
