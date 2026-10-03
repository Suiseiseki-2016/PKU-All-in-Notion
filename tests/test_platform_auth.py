"""Product email+password auth client (register / login / redeem / forgot)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from pku_sync import platform
from pku_sync.panel.platform_bridge import RealPlatformBridge


def settings(tmp_path, **overrides):
    base = dict(
        cloud_transcribe_url="https://pku.aeoluswu.info/v1/transcribe",
        platform_token="",
        transcription_backend="local",
        data_dir=tmp_path,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_register_persists_platform_token_locally(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setattr(platform, "env_file_path", lambda _: tmp_path / ".env")
    monkeypatch.setattr(
        platform.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={
                "platform_token": "sess-register",
                "email": "Student@Example.com",
                "email_verified": False,
            },
        ),
    )

    result = platform.register("Student@Example.com", "password1", s)

    assert result == {
        "email": "student@example.com",
        "email_verified": False,
        "active": True,
    }
    assert s.platform_token == "sess-register"
    text = (tmp_path / ".env").read_text("utf-8")
    assert "PLATFORM_TOKEN=sess-register" in text
    assert "CLOUD_TRANSCRIBE_URL=" in text


def test_login_persists_session_like_activate(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setattr(platform, "env_file_path", lambda _: tmp_path / ".env")
    monkeypatch.setattr(
        platform.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={
                "platform_token": "sess-login",
                "email": "a@b.c",
                "email_verified": True,
            },
        ),
    )

    result = platform.login("a@b.c", "password1", s)

    assert result["email_verified"] is True
    assert s.platform_token == "sess-login"
    assert "PLATFORM_TOKEN=sess-login" in (tmp_path / ".env").read_text("utf-8")


def test_login_surfaces_relay_detail_and_status(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setattr(platform, "env_file_path", lambda _: tmp_path / ".env")
    monkeypatch.setattr(
        platform.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            401, json={"detail": "邮箱或密码不正确"}
        ),
    )

    with pytest.raises(platform.PlatformError, match="邮箱或密码不正确") as caught:
        platform.login("a@b.c", "wrong", s)
    assert caught.value.status_code == 401
    assert s.platform_token == ""


def test_panel_login_reconciles_account_profile(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setattr(
        platform,
        "login",
        lambda email, password, settings: {
            "email": email,
            "email_verified": True,
            "active": True,
        },
    )
    monkeypatch.setattr(
        platform,
        "sync_account_profile",
        lambda settings: {"available": True, "status": "pulled", "revision": 3},
    )

    result = RealPlatformBridge(s).login("a@b.c", "password1")

    assert result["profile_sync"] == {
        "available": True,
        "status": "pulled",
        "revision": 3,
    }


def test_existing_panel_session_reconciles_profile_once(tmp_path, monkeypatch):
    s = settings(tmp_path, platform_token="session")
    calls = []
    monkeypatch.setattr(
        platform,
        "auth_me",
        lambda settings: {
            "active": True,
            "available": True,
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        platform,
        "sync_account_profile",
        lambda settings: calls.append(settings) or {"status": "current"},
    )
    bridge = RealPlatformBridge(s)

    assert bridge.me()["profile_sync"] == {"status": "current"}
    assert "profile_sync" not in bridge.me()
    assert calls == [s]


def test_logout_clears_local_token(tmp_path, monkeypatch):
    s = settings(tmp_path, platform_token="sess-out")
    monkeypatch.setattr(platform, "env_file_path", lambda _: tmp_path / ".env")
    (tmp_path / ".env").write_text("PLATFORM_TOKEN=sess-out\n", encoding="utf-8")
    seen = {}

    def fake_post(url, headers=None, timeout=None, **kwargs):
        seen["url"] = url
        seen["auth"] = (headers or {}).get("Authorization")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(platform.httpx, "post", fake_post)

    assert platform.logout(s) == {"ok": True}
    assert s.platform_token == ""
    assert "PLATFORM_TOKEN=\n" in (tmp_path / ".env").read_text("utf-8")
    assert seen["auth"] == "Bearer sess-out"
    assert seen["url"].endswith("/v1/auth/logout")


def test_forgot_password_only_triggers_api(tmp_path, monkeypatch):
    s = settings(tmp_path)
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(platform.httpx, "post", fake_post)
    assert platform.forgot_password("user@example.com", s) == {"ok": True}
    assert calls[0]["url"].endswith("/v1/auth/forgot")
    assert calls[0]["json"] == {"email": "user@example.com"}


def test_redeem_requires_session_and_persists_cloud_backend(tmp_path, monkeypatch):
    s = settings(tmp_path, platform_token="sess-1")
    monkeypatch.setattr(platform, "env_file_path", lambda _: tmp_path / ".env")
    monkeypatch.setattr(
        platform.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            200, json={"transcribe_seconds_remaining": 1200}
        ),
    )

    result = platform.redeem(" CODE99 ", s)

    assert result == {"transcribe_seconds_remaining": 1200}
    assert s.transcription_backend == "cloud"
    assert "TRANSCRIPTION_BACKEND=cloud" in (tmp_path / ".env").read_text("utf-8")


def test_redeem_gates_unverified_with_403(tmp_path, monkeypatch):
    s = settings(tmp_path, platform_token="sess-1")
    monkeypatch.setattr(platform, "env_file_path", lambda _: tmp_path / ".env")
    monkeypatch.setattr(
        platform.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            403, json={"detail": "请先完成邮箱验证后再使用转写、AI 或兑换额度"}
        ),
    )

    with pytest.raises(platform.PlatformError, match="邮箱验证") as caught:
        platform.redeem("CODE99", s)
    assert caught.value.status_code == 403


def test_auth_me_and_quota_pass_email_verified(tmp_path, monkeypatch):
    s = settings(tmp_path, platform_token="sess-1")

    def fake_get(url, headers, timeout):
        return httpx.Response(
            200,
            json={
                "email": "a@b.c",
                "email_verified": False,
                "legacy_activate": False,
                "transcribe_seconds_remaining": 60,
                "llm_points_remaining": 1.5,
            },
        )

    monkeypatch.setattr(platform.httpx, "get", fake_get)
    me = platform.auth_me(s)
    assert me["email"] == "a@b.c"
    assert me["email_verified"] is False
    assert "platform_token" not in me

    quota = platform.quota(s)
    assert quota["email"] == "a@b.c"
    assert quota["email_verified"] is False
    assert quota["active"] is True


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, "兑换码无效或已使用"),
        (401, "登录已过期，请重新登录后兑换"),
        (403, "请先完成邮箱验证后再兑换额度"),
        (429, "尝试次数过多，请稍后再试"),
        (503, "云端服务暂时不可用，请稍后重试"),
    ],
)
def test_redeem_errors_are_actionable_chinese(tmp_path, monkeypatch, status, expected):
    s = settings(tmp_path, platform_token="sess-1")
    monkeypatch.setattr(
        platform.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            status, json={"detail": "private upstream English detail"}
        ),
    )
    with pytest.raises(platform.PlatformError) as caught:
        platform.redeem("TEST1234", s)
    assert str(caught.value) == expected
    assert caught.value.status_code == status
    assert "private upstream" not in str(caught.value)
