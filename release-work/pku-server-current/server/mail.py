"""Transactional email for product accounts (verify / password reset).

Providers are selected by ``MAIL_PROVIDER``:

- ``resend`` — POST https://api.resend.com/emails (preferred).
- ``log`` — write the message to stdout; used in tests and local dry-runs.

The open client never holds these credentials. Only the operated relay
reads ``RESEND_API_KEY`` / ``MAIL_FROM`` / ``APP_PUBLIC_URL`` from the
server environment (docs/PRODUCT_PLAN.md in the client repo).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urljoin

import httpx

RESEND_API = "https://api.resend.com/emails"
DEFAULT_TOKEN_TTL_SECONDS = 30 * 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


class MailError(RuntimeError):
    """Safe, operator-facing mail failure (never includes the API key)."""


@dataclass(frozen=True)
class MailConfig:
    provider: str
    api_key: str
    mail_from: str
    app_public_url: str
    token_secret: str
    token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS

    @classmethod
    def from_environment(cls) -> MailConfig | None:
        """Build config when mail is configured; return None if unused.

        ``MAIL_PROVIDER=log`` needs only ``MAIL_FROM`` (and optionally
        ``APP_PUBLIC_URL``). ``resend`` also needs ``RESEND_API_KEY``.
        """
        provider = (os.environ.get("MAIL_PROVIDER") or "").strip().lower()
        if not provider:
            return None
        mail_from = (os.environ.get("MAIL_FROM") or "").strip()
        if not mail_from:
            raise MailError("MAIL_FROM is required when MAIL_PROVIDER is set")
        api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
        if provider == "resend" and not api_key:
            raise MailError("RESEND_API_KEY is required when MAIL_PROVIDER=resend")
        if provider not in {"resend", "log"}:
            raise MailError(f"unsupported MAIL_PROVIDER: {provider!r}")
        app_public_url = (os.environ.get("APP_PUBLIC_URL") or "").strip().rstrip("/")
        # Prefer an explicit signing secret; fall back to the API key so a
        # misconfigured deploy still produces opaque tokens (not reversible
        # without the key). Log mode uses a fixed local secret.
        token_secret = (os.environ.get("MAIL_TOKEN_SECRET") or api_key or "local-dev-mail-secret")
        try:
            ttl = int(os.environ.get("MAIL_TOKEN_TTL_SECONDS") or DEFAULT_TOKEN_TTL_SECONDS)
        except ValueError as exc:
            raise MailError("MAIL_TOKEN_TTL_SECONDS must be an integer") from exc
        return cls(
            provider=provider,
            api_key=api_key,
            mail_from=mail_from,
            app_public_url=app_public_url,
            token_secret=token_secret,
            token_ttl_seconds=ttl,
        )


@dataclass(frozen=True)
class SentMail:
    provider: str
    to: str
    subject: str
    id: str


class MailSender(Protocol):
    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> SentMail:
        ...


class LogMailSender:
    """Dev/test sender: never leaves the process."""

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> SentMail:
        print(f"[mail:log] to={to} subject={subject}\n{text}")
        return SentMail(provider="log", to=to, subject=subject, id=f"log-{secrets.token_hex(8)}")


class ResendMailSender:
    def __init__(self, api_key: str, mail_from: str, *, client: httpx.Client | None = None):
        self._api_key = api_key
        self._mail_from = mail_from
        self._client = client

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> SentMail:
        payload: dict = {
            "from": self._mail_from,
            "to": [to],
            "subject": subject,
            "text": text,
        }
        if html:
            payload["html"] = html
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(RESEND_API, headers=headers, json=payload)
            else:
                with httpx.Client(timeout=30.0) as client:
                    response = client.post(RESEND_API, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise MailError("无法连接邮件服务，请稍后重试") from exc
        if response.status_code >= 400:
            # Resend body may contain the recipient; keep the public message
            # generic and never echo Authorization headers.
            raise MailError(f"邮件发送失败（HTTP {response.status_code}）")
        body = response.json()
        mail_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(mail_id, str) or not mail_id:
            raise MailError("邮件服务返回异常")
        return SentMail(provider="resend", to=to, subject=subject, id=mail_id)


def make_sender(config: MailConfig, *, client: httpx.Client | None = None) -> MailSender:
    if config.provider == "log":
        return LogMailSender()
    if config.provider == "resend":
        return ResendMailSender(config.api_key, config.mail_from, client=client)
    raise MailError(f"unsupported MAIL_PROVIDER: {config.provider!r}")


def issue_email_token(
    config: MailConfig,
    *,
    purpose: str,
    email: str,
    now: float | None = None,
) -> str:
    """Mint a signed token: purpose.<b64url email>.exp.nonce.sig.

    The email is base64url-encoded so dots inside addresses cannot break
    the dotted token layout.
    """
    if purpose not in {"verify", "reset"}:
        raise MailError(f"unsupported token purpose: {purpose!r}")
    issued = int(now if now is not None else time.time())
    exp = issued + int(config.token_ttl_seconds)
    nonce = secrets.token_urlsafe(16)
    email_norm = email.strip().lower()
    email_part = _b64url(email_norm.encode("utf-8"))
    body = f"{purpose}.{email_part}.{exp}.{nonce}"
    sig = hmac.new(
        config.token_secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{body}.{sig}"


def verify_email_token(
    config: MailConfig,
    token: str,
    *,
    purpose: str,
    now: float | None = None,
) -> str:
    """Return the normalized email if the token is valid for ``purpose``."""
    parts = (token or "").split(".")
    if len(parts) != 5:
        raise MailError("验证链接无效或已过期")
    token_purpose, email_part, exp_s, nonce, sig = parts
    if token_purpose != purpose:
        raise MailError("验证链接无效或已过期")
    body = f"{token_purpose}.{email_part}.{exp_s}.{nonce}"
    expected = hmac.new(
        config.token_secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise MailError("验证链接无效或已过期")
    try:
        exp = int(exp_s)
        email = _b64url_decode(email_part).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise MailError("验证链接无效或已过期") from exc
    if (now if now is not None else time.time()) > exp:
        raise MailError("验证链接无效或已过期")
    if not email or "@" not in email:
        raise MailError("验证链接无效或已过期")
    return email


def build_action_url(config: MailConfig, *, path: str, token: str) -> str:
    if not config.app_public_url:
        raise MailError("APP_PUBLIC_URL is required to build verification links")
    base = config.app_public_url.rstrip("/") + "/"
    return urljoin(base, path.lstrip("/")) + "?" + urlencode({"token": token})


def send_verification_email(
    sender: MailSender,
    config: MailConfig,
    *,
    to: str,
    token: str | None = None,
) -> SentMail:
    token = token or issue_email_token(config, purpose="verify", email=to)
    link = build_action_url(config, path="/verify", token=token)
    subject = "验证你的 PKU All in Notion 邮箱"
    text = (
        "你好，\n\n"
        "请点击下面的链接完成邮箱验证（约 30 分钟内有效）：\n"
        f"{link}\n\n"
        "如果这不是你本人的操作，请忽略本邮件。\n"
    )
    html = (
        "<p>你好，</p>"
        "<p>请点击下面的链接完成邮箱验证（约 30 分钟内有效）：</p>"
        f'<p><a href="{link}">验证邮箱</a></p>'
        f"<p style=\"color:#71827e;font-size:12px;word-break:break-all\">{link}</p>"
        "<p>如果这不是你本人的操作，请忽略本邮件。</p>"
    )
    return sender.send(to=to, subject=subject, text=text, html=html)


def send_password_reset_email(
    sender: MailSender,
    config: MailConfig,
    *,
    to: str,
    token: str | None = None,
) -> SentMail:
    token = token or issue_email_token(config, purpose="reset", email=to)
    link = build_action_url(config, path="/reset", token=token)
    subject = "重置 PKU All in Notion 密码"
    text = (
        "你好，\n\n"
        "请点击下面的链接重置密码（约 30 分钟内有效）：\n"
        f"{link}\n\n"
        "如果这不是你本人的操作，请忽略本邮件。\n"
    )
    html = (
        "<p>你好，</p>"
        "<p>请点击下面的链接重置密码（约 30 分钟内有效）：</p>"
        f'<p><a href="{link}">重置密码</a></p>'
        f"<p style=\"color:#71827e;font-size:12px;word-break:break-all\">{link}</p>"
        "<p>如果这不是你本人的操作，请忽略本邮件。</p>"
    )
    return sender.send(to=to, subject=subject, text=text, html=html)
