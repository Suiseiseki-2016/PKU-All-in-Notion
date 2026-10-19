"""Product email+password accounts (register / login / verify / reset).

Coexists with the legacy activate-by-code path: anonymous accounts keep
``email`` NULL and stay usable for the pilot. Product accounts must
verify email before redeem, transcription, or LLM calls
(docs/PRODUCT_PLAN.md in the client repo).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError
except ImportError:  # pragma: no cover
    class VerifyMismatchError(Exception):
        pass

    class PasswordHasher:  # type: ignore[no-redef]
        def hash(self, password: str) -> str:
            salt = secrets.token_hex(16)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
            return f"pbkdf2_sha256${salt}${digest}"

        def verify(self, password_hash: str, password: str) -> bool:
            parts = (password_hash or "").split("$")
            if len(parts) != 3 or parts[0] != "pbkdf2_sha256":
                raise VerifyMismatchError("invalid hash")
            _, salt, expected = parts
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
            if not hmac.compare_digest(actual, expected):
                raise VerifyMismatchError("mismatch")
            return True

from . import accounts, mail

_HASHER = PasswordHasher()
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8


class AuthError(RuntimeError):
    """Safe product-facing auth failure."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AuthResult:
    token: str
    email: str
    email_verified: bool
    account_id: int
    invite_code: str = ""
    tier: str = "lite"


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not normalized or not _EMAIL_RE.match(normalized) or len(normalized) > 254:
        raise AuthError("请输入有效的邮箱地址")
    return normalized


def _validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
    if len(password) > 128:
        raise AuthError("密码过长")
    return password


def hash_password(password: str) -> str:
    return _HASHER.hash(_validate_password(password))


def check_password(password_hash: str, password: str) -> bool:
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, TypeError, ValueError):
        return False


def ensure_product_auth_schema(conn: sqlite3.Connection) -> None:
    """Idempotent columns for email/password product accounts."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    columns = {
        "email": "TEXT",
        "password_hash": "TEXT",
        "email_verified_at": "REAL",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")
    # Unique email when present (SQLite allows multiple NULLs).
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS accounts_email_unique"
        " ON accounts(email) WHERE email IS NOT NULL"
    )


def email_is_verified(account: sqlite3.Row | dict) -> bool:
    """Legacy activate accounts (no email) count as unrestricted for now."""
    email = account["email"] if "email" in account.keys() else None
    if not email:
        return True
    verified = account["email_verified_at"] if "email_verified_at" in account.keys() else None
    return verified is not None


def require_verified(account: sqlite3.Row | dict) -> None:
    if not email_is_verified(account):
        raise AuthError(
            "请先完成邮箱验证后再使用转写、AI 或兑换额度",
            status_code=403,
        )


def find_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM accounts WHERE email = ?",
        (normalize_email(email),),
    ).fetchone()


def revoke_sessions(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
    conn.commit()


def register_account(
    conn: sqlite3.Connection,
    *,
    email: str,
    password: str,
    mail_config: mail.MailConfig | None,
    mail_sender: mail.MailSender | None,
    invite_code: str | None = None,
) -> AuthResult:
    """Create an unverified product account and email a verify link."""
    email_norm = _validate_email(email)
    password_hash = hash_password(password)
    if find_by_email(conn, email_norm) is not None:
        raise AuthError("该邮箱已注册", status_code=409)
    if mail_config is None or mail_sender is None:
        raise AuthError("邮件服务未配置，暂时无法注册", status_code=503)
    if not mail_config.app_public_url:
        raise AuthError("邮件服务未配置，暂时无法注册", status_code=503)

    now = time.time()
    label = email_norm
    own_invite_code = f"PKU-{secrets.token_hex(4).upper()}"
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO accounts (label, email, password_hash, email_verified_at,"
                " tier, transcribe_seconds_remaining, monthly_transcribe_seconds,"
                " llm_millipoints_remaining, monthly_llm_millipoints,"
                " period_started_at, created_at, invite_code)"
                " VALUES (?, ?, ?, NULL, 'lite', 0, ?, 0, ?, ?, ?, ?)",
                (
                    label,
                    email_norm,
                    password_hash,
                    0,
                    0,
                    now,
                    now,
                    own_invite_code,
                ),
            )
            account_id = int(cur.lastrowid)
            token = secrets.token_hex(24)
            conn.execute(
                "INSERT INTO sessions (token, account_id, issued_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (token, account_id, now, now + accounts.SESSION_TTL_SECONDS),
            )
    except sqlite3.IntegrityError as exc:
        raise AuthError("该邮箱已注册", status_code=409) from exc

    if invite_code and invite_code.strip():
        accounts.apply_invite_code(conn, account_id, invite_code)

    try:
        mail.send_verification_email(mail_sender, mail_config, to=email_norm)
    except mail.MailError as exc:
        # Account exists; caller can resend. Surface a clear send failure.
        raise AuthError(f"账号已创建，但验证邮件发送失败：{exc}", status_code=502) from exc

    return AuthResult(
        token=token,
        email=email_norm,
        email_verified=False,
        account_id=account_id,
        invite_code=own_invite_code,
        tier="lite",
    )


def login_account(conn: sqlite3.Connection, *, email: str, password: str) -> AuthResult:
    email_norm = _validate_email(email)
    row = find_by_email(conn, email_norm)
    # Constant-ish failure copy: do not reveal whether the email exists.
    if row is None or not row["password_hash"] or not check_password(row["password_hash"], password):
        raise AuthError("邮箱或密码不正确", status_code=401)
    if row["status"] != "active":
        raise AuthError("账号不可用", status_code=403)
    account_id = int(row["id"])
    invite_code = accounts.ensure_invite_code(conn, account_id)
    tier = (row["tier"] if "tier" in row.keys() and row["tier"] else "lite")
    token = accounts.issue_session(conn, account_id)
    return AuthResult(
        token=token,
        email=email_norm,
        email_verified=row["email_verified_at"] is not None,
        account_id=account_id,
        invite_code=invite_code,
        tier=tier,
    )


def logout_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()


def verify_email_with_token(
    conn: sqlite3.Connection,
    mail_config: mail.MailConfig,
    token: str,
) -> str:
    email = mail.verify_email_token(mail_config, token, purpose="verify")
    row = find_by_email(conn, email)
    if row is None:
        raise AuthError("验证链接无效或已过期")
    if row["email_verified_at"] is None:
        conn.execute(
            "UPDATE accounts SET email_verified_at = ? WHERE id = ?",
            (time.time(), int(row["id"])),
        )
        conn.commit()
    return email


def resend_verification(
    conn: sqlite3.Connection,
    *,
    email: str,
    mail_config: mail.MailConfig | None,
    mail_sender: mail.MailSender | None,
) -> None:
    email_norm = _validate_email(email)
    if mail_config is None or mail_sender is None or not mail_config.app_public_url:
        raise AuthError("邮件服务未配置", status_code=503)
    row = find_by_email(conn, email_norm)
    # Always succeed from the caller's perspective when the address is unknown
    # or already verified, to avoid account enumeration.
    if row is None or row["email_verified_at"] is not None or not row["password_hash"]:
        return
    try:
        mail.send_verification_email(mail_sender, mail_config, to=email_norm)
    except mail.MailError as exc:
        raise AuthError(str(exc), status_code=502) from exc


def request_password_reset(
    conn: sqlite3.Connection,
    *,
    email: str,
    mail_config: mail.MailConfig | None,
    mail_sender: mail.MailSender | None,
) -> None:
    email_norm = _validate_email(email)
    if mail_config is None or mail_sender is None or not mail_config.app_public_url:
        raise AuthError("邮件服务未配置", status_code=503)
    row = find_by_email(conn, email_norm)
    if row is None or not row["password_hash"]:
        return
    try:
        mail.send_password_reset_email(mail_sender, mail_config, to=email_norm)
    except mail.MailError as exc:
        raise AuthError(str(exc), status_code=502) from exc


def reset_password_with_token(
    conn: sqlite3.Connection,
    mail_config: mail.MailConfig,
    *,
    token: str,
    password: str,
) -> str:
    email = mail.verify_email_token(mail_config, token, purpose="reset")
    row = find_by_email(conn, email)
    if row is None or not row["password_hash"]:
        raise AuthError("重置链接无效或已过期")
    password_hash = hash_password(password)
    with conn:
        conn.execute(
            "UPDATE accounts SET password_hash = ? WHERE id = ?",
            (password_hash, int(row["id"])),
        )
        conn.execute("DELETE FROM sessions WHERE account_id = ?", (int(row["id"]),))
    return email


def account_public_view(account: sqlite3.Row) -> dict:
    keys = set(account.keys())
    email = account["email"] if "email" in keys else None
    verified_at = account["email_verified_at"] if "email_verified_at" in keys else None
    tier = (account["tier"] if "tier" in keys and account["tier"] else ("pro" if email is None else "lite")).lower()
    invite_code = account["invite_code"] if "invite_code" in keys and account["invite_code"] else ""
    campus_bound = bool(account["campus_id_hash"]) if "campus_id_hash" in keys else False
    return {
        "email": email,
        "email_verified": bool(email and verified_at is not None),
        "label": account["label"],
        "legacy_activate": email is None,
        "tier": tier,
        "invite_code": invite_code,
        "campus_bound": campus_bound,
    }
