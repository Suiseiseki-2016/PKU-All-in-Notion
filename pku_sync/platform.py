"""Local-only bridge between the student panel and the operated relay."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .envfile import write_env_values
from .notion_login import env_file_path

DEFAULT_TRANSCRIBE_URL = "https://pku.aeoluswu.info/v1/transcribe"


class PlatformError(RuntimeError):
    """A safe, student-facing relay error."""


def _endpoint(transcribe_url: str, endpoint: str) -> str:
    parts = urlsplit(transcribe_url or DEFAULT_TRANSCRIBE_URL)
    return urlunsplit((parts.scheme, parts.netloc, endpoint, "", ""))


def activate(code: str, settings) -> dict:
    """Redeem one code and keep the returned session token in local .env only."""
    clean_code = code.strip()
    if not clean_code:
        raise PlatformError("请输入兑换码")
    url = _endpoint(settings.cloud_transcribe_url, "/v1/activate")
    try:
        response = httpx.post(url, json={"code": clean_code}, timeout=30)
    except httpx.HTTPError as exc:
        raise PlatformError("无法连接云端服务，请稍后重试") from exc
    if response.status_code != 200:
        raise PlatformError("兑换码无效或已使用")
    payload = response.json()
    token = payload.get("platform_token")
    seconds = payload.get("transcribe_seconds_remaining")
    if not isinstance(token, str) or not isinstance(seconds, int):
        raise PlatformError("云端返回异常，请联系管理员")
    transcribe_url = settings.cloud_transcribe_url or DEFAULT_TRANSCRIBE_URL
    write_env_values(
        env_file_path(settings),
        {
            "CLOUD_TRANSCRIBE_URL": transcribe_url,
            "PLATFORM_TOKEN": token,
            "TRANSCRIPTION_BACKEND": "cloud",
        },
    )
    settings.cloud_transcribe_url = transcribe_url
    settings.platform_token = token
    settings.transcription_backend = "cloud"
    return {"transcribe_seconds_remaining": seconds}


def quota(settings) -> dict:
    """Get the current cloud transcription balance without exposing the token."""
    if not settings.platform_token:
        return {"active": False}
    try:
        response = httpx.get(
            _endpoint(settings.cloud_transcribe_url, "/v1/quota"),
            headers={"Authorization": f"Bearer {settings.platform_token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        return {"active": True, "available": False}
    if response.status_code != 200:
        return {"active": True, "available": False}
    payload = response.json()
    return {
        "active": True,
        "available": True,
        "transcribe_seconds_remaining": payload.get("transcribe_seconds_remaining"),
    }
