from __future__ import annotations

from types import SimpleNamespace

import httpx

from pku_sync import platform


def settings(tmp_path):
    return SimpleNamespace(
        cloud_transcribe_url="https://pku.aeoluswu.info/v1/transcribe",
        platform_token="",
        transcription_backend="local",
    )


def test_activate_writes_only_local_platform_settings(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setattr(platform, "env_file_path", lambda _: tmp_path / ".env")
    monkeypatch.setattr(
        platform.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            200, json={"platform_token": "opaque-token", "transcribe_seconds_remaining": 900}
        ),
    )

    result = platform.activate(" CODE123 ", s)

    assert result == {"transcribe_seconds_remaining": 900}
    assert s.platform_token == "opaque-token"
    assert s.transcription_backend == "cloud"
    text = (tmp_path / ".env").read_text("utf-8")
    assert "PLATFORM_TOKEN=opaque-token" in text
    assert "TRANSCRIPTION_BACKEND=cloud" in text


def test_quota_does_not_expose_platform_token(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "opaque-token"
    seen = {}

    def fake_get(url, headers, timeout):
        seen.update(headers)
        return httpx.Response(200, json={"transcribe_seconds_remaining": 3600})

    monkeypatch.setattr(platform.httpx, "get", fake_get)
    assert platform.quota(s) == {
        "active": True,
        "available": True,
        "transcribe_seconds_remaining": 3600,
    }
    assert seen["Authorization"] == "Bearer opaque-token"
