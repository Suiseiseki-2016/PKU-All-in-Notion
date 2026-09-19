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



def llm(operation: str, prompt: str, settings) -> dict:
    """Run one approved metered LLM operation through the relay."""
    token = (settings.platform_token or "").strip()
    if not token:
        raise PlatformError("云端登录已失效，请重新激活后重试。")
    try:
        response = httpx.post(
            _endpoint(settings.cloud_transcribe_url, "/v1/llm"),
            headers={"Authorization": f"Bearer {token}"},
            json={"operation": operation, "system": "你是课程练习整理助手。只返回请求指定的 JSON。", "user": prompt},
            timeout=settings.llm_timeout,
        )
    except httpx.HTTPError as exc:
        raise PlatformError("无法连接云端服务，请稍后重试") from exc
    if response.status_code != 200:
        safe = {
            401: "云端登录已失效，请重新激活后重试。",
            402: "AI 点不足，请兑换新额度后重试。",
            502: "AI 服务暂时没有完成请求，请稍后重试。",
            503: "AI 服务暂未配置，请联系管理员后重试。",
        }
        error = PlatformError(safe.get(response.status_code, "云端服务暂时不可用，请稍后重试。"))
        error.status_code = response.status_code
        raise error
    payload = response.json()
    if not isinstance(payload.get("content"), str) or not isinstance(payload.get("points_charged"), (int, float)):
        raise PlatformError("云端返回异常，请联系管理员")
    return {"content": payload["content"], "points_charged": payload["points_charged"]}

def quota(settings) -> dict:
    """Get the current cloud transcription balance without exposing the token."""
    token = (settings.platform_token or "").strip()
    if not token:
        return {"active": False}
    try:
        response = httpx.get(
            _endpoint(settings.cloud_transcribe_url, "/v1/quota"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        return {"active": True, "available": False}
    if response.status_code != 200:
        return {"active": True, "available": False}
    payload = response.json()
    result = {
        "active": True,
        "available": True,
        "transcribe_seconds_remaining": payload.get("transcribe_seconds_remaining"),
    }
    # LLM points surface alongside transcription seconds when the relay
    # provides them (it always does); a missing key is never fabricated.
    if payload.get("llm_points_remaining") is not None:
        result["llm_points_remaining"] = payload.get("llm_points_remaining")
    return result
