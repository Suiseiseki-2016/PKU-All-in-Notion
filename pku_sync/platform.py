"""Local-only bridge between the student panel and the operated relay."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .envfile import write_env_values
from .notion_login import env_file_path
from .network import dead_loopback_proxy

DEFAULT_TRANSCRIBE_URL = "https://pku.aeoluswu.info/v1/transcribe"


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """Retry directly if an inherited localhost proxy has gone away."""
    sender = httpx.post if method == "POST" else httpx.get
    try:
        return sender(url, **kwargs)
    except httpx.TransportError:
        if not dead_loopback_proxy():
            raise
        with httpx.Client(trust_env=False) as client:
            return client.request(method, url, **kwargs)


def _post(url: str, **kwargs) -> httpx.Response:
    return _request("POST", url, **kwargs)


def _get(url: str, **kwargs) -> httpx.Response:
    return _request("GET", url, **kwargs)


class PlatformError(RuntimeError):
    """A safe, student-facing relay error."""


def _endpoint(transcribe_url: str, endpoint: str) -> str:
    parts = urlsplit(transcribe_url or DEFAULT_TRANSCRIBE_URL)
    return urlunsplit((parts.scheme, parts.netloc, endpoint, "", ""))


def _relay_detail(response: httpx.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return fallback
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, str) and detail.strip() else fallback


def _raise_platform(response: httpx.Response, fallback: str) -> None:
    error = PlatformError(_relay_detail(response, fallback))
    error.status_code = response.status_code
    raise error


def _persist_session(settings, token: str, *, cloud_backend: bool = False) -> None:
    """Write the platform session token the same way activation does."""
    transcribe_url = settings.cloud_transcribe_url or DEFAULT_TRANSCRIBE_URL
    values = {
        "CLOUD_TRANSCRIBE_URL": transcribe_url,
        "PLATFORM_TOKEN": token,
    }
    if cloud_backend:
        values["TRANSCRIPTION_BACKEND"] = "cloud"
        settings.transcription_backend = "cloud"
    write_env_values(env_file_path(settings), values)
    settings.cloud_transcribe_url = transcribe_url
    settings.platform_token = token


def _clear_session(settings) -> None:
    write_env_values(env_file_path(settings), {"PLATFORM_TOKEN": ""})
    settings.platform_token = ""


def _auth_session_payload(payload: dict, settings) -> dict:
    token = payload.get("platform_token")
    email = payload.get("email")
    verified = payload.get("email_verified")
    if not isinstance(token, str) or not token.strip():
        raise PlatformError("云端返回异常，请联系管理员")
    if not isinstance(email, str) or not email.strip():
        raise PlatformError("云端返回异常，请联系管理员")
    if not isinstance(verified, bool):
        raise PlatformError("云端返回异常，请联系管理员")
    _persist_session(settings, token.strip())
    return {
        "email": email.strip().lower(),
        "email_verified": verified,
        "active": True,
    }


def activate(code: str, settings) -> dict:
    """Redeem one code and keep the returned session token in local .env only.

    Legacy pilot path: the code creates an anonymous session. Product accounts
    should use ``redeem`` after email+password login instead.
    """
    clean_code = code.strip()
    if not clean_code:
        raise PlatformError("请输入兑换码")
    url = _endpoint(settings.cloud_transcribe_url, "/v1/activate")
    try:
        response = _post(url, json={"code": clean_code}, timeout=30)
    except httpx.HTTPError as exc:
        raise PlatformError("无法连接云端服务，请稍后重试") from exc
    if response.status_code != 200:
        raise PlatformError("兑换码无效或已使用")
    payload = response.json()
    token = payload.get("platform_token")
    seconds = payload.get("transcribe_seconds_remaining")
    if not isinstance(token, str) or not isinstance(seconds, int):
        raise PlatformError("云端返回异常，请联系管理员")
    _persist_session(settings, token, cloud_backend=True)
    return {"transcribe_seconds_remaining": seconds}


def register(email: str, password: str, settings) -> dict:
    """Create a product account; session token is stored locally like activate."""
    url = _endpoint(settings.cloud_transcribe_url, "/v1/auth/register")
    try:
        response = _post(
            url, json={"email": email, "password": password}, timeout=30
        )
    except httpx.HTTPError as exc:
        raise PlatformError("无法连接云端服务，请稍后重试") from exc
    if response.status_code != 200:
        _raise_platform(response, "注册没有成功，请稍后重试")
    return _auth_session_payload(response.json(), settings)


def login(email: str, password: str, settings) -> dict:
    """Log in and persist the returned platform session token locally."""
    url = _endpoint(settings.cloud_transcribe_url, "/v1/auth/login")
    try:
        response = _post(
            url, json={"email": email, "password": password}, timeout=30
        )
    except httpx.HTTPError as exc:
        raise PlatformError("无法连接云端服务，请稍后重试") from exc
    if response.status_code != 200:
        _raise_platform(response, "登录没有成功，请稍后重试")
    return _auth_session_payload(response.json(), settings)


def logout(settings) -> dict:
    """Revoke the remote session when possible and clear the local token."""
    token = (settings.platform_token or "").strip()
    if token:
        try:
            _post(
                _endpoint(settings.cloud_transcribe_url, "/v1/auth/logout"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except httpx.HTTPError:
            # Local clear is the product promise; remote revoke is best-effort.
            pass
    _clear_session(settings)
    return {"ok": True}


def auth_me(settings) -> dict:
    """Current product account view; never returns the session token."""
    token = (settings.platform_token or "").strip()
    if not token:
        return {"active": False}
    try:
        response = _get(
            _endpoint(settings.cloud_transcribe_url, "/v1/auth/me"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        return {"active": True, "available": False}
    if response.status_code == 401:
        _clear_session(settings)
        return {"active": False}
    if response.status_code != 200:
        return {"active": True, "available": False}
    payload = response.json()
    result = {
        "active": True,
        "available": True,
        "email": payload.get("email"),
        "email_verified": bool(payload.get("email_verified")),
        "legacy_activate": bool(payload.get("legacy_activate")),
    }
    if payload.get("transcribe_seconds_remaining") is not None:
        result["transcribe_seconds_remaining"] = payload.get(
            "transcribe_seconds_remaining"
        )
    if payload.get("llm_points_remaining") is not None:
        result["llm_points_remaining"] = payload.get("llm_points_remaining")
    return result


def forgot_password(email: str, settings) -> dict:
    """Ask the relay to email a one-time reset link (landing is server-side)."""
    url = _endpoint(settings.cloud_transcribe_url, "/v1/auth/forgot")
    try:
        response = _post(url, json={"email": email}, timeout=30)
    except httpx.HTTPError as exc:
        raise PlatformError("无法连接云端服务，请稍后重试") from exc
    if response.status_code != 200:
        _raise_platform(response, "重置邮件发送失败，请稍后重试")
    return {"ok": True}


def resend_verification(email: str, settings) -> dict:
    """Resend the email verification link for an unverified product account."""
    url = _endpoint(settings.cloud_transcribe_url, "/v1/auth/resend-verification")
    try:
        response = _post(url, json={"email": email}, timeout=30)
    except httpx.HTTPError as exc:
        raise PlatformError("无法连接云端服务，请稍后重试") from exc
    if response.status_code != 200:
        _raise_platform(response, "验证邮件发送失败，请稍后重试")
    return {"ok": True}


def redeem(code: str, settings) -> dict:
    """Apply a recharge code to the logged-in product account (requires verify)."""
    clean_code = code.strip()
    if not clean_code:
        raise PlatformError("请输入兑换码")
    token = (settings.platform_token or "").strip()
    if not token:
        raise PlatformError("请先登录后再兑换额度")
    try:
        response = _post(
            _endpoint(settings.cloud_transcribe_url, "/v1/redeem"),
            headers={"Authorization": f"Bearer {token}"},
            json={"code": clean_code},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PlatformError("无法连接云端服务，请稍后重试") from exc
    if response.status_code != 200:
        messages = {
            400: "兑换码无效或已使用",
            401: "登录已过期，请重新登录后兑换",
            403: "请先完成邮箱验证后再兑换额度",
            429: "尝试次数过多，请稍后再试",
            502: "云端服务暂时不可用，请稍后重试",
            503: "云端服务暂时不可用，请稍后重试",
        }
        error = PlatformError(messages.get(response.status_code, "兑换没有成功，请稍后重试"))
        error.status_code = response.status_code
        raise error
    payload = response.json()
    seconds = payload.get("transcribe_seconds_remaining")
    if not isinstance(seconds, int):
        raise PlatformError("云端返回异常，请联系管理员")
    # Product redeem implies cloud usage; mirror activate's local backend flip.
    write_env_values(
        env_file_path(settings),
        {
            "CLOUD_TRANSCRIBE_URL": settings.cloud_transcribe_url
            or DEFAULT_TRANSCRIBE_URL,
            "TRANSCRIPTION_BACKEND": "cloud",
        },
    )
    settings.transcription_backend = "cloud"
    return {"transcribe_seconds_remaining": seconds}


_DEFAULT_LLM_SYSTEM = "你是课程练习整理助手。只返回请求指定的 JSON。"
_LLM_JOB_POLL_SECONDS = 2.5
_LLM_JOB_DEADLINE_SECONDS = 12 * 60


def _llm_error(response: httpx.Response) -> PlatformError:
    safe = {
        401: "云端登录已失效，请重新激活后重试。",
        402: "AI 点不足，请兑换新额度后重试。",
        403: "请先完成邮箱验证后再使用转写、AI 或兑换额度",
        502: "AI 服务暂时没有完成请求，请稍后重试。",
        503: "AI 服务暂未配置，请联系管理员后重试。",
    }
    error = PlatformError(safe.get(response.status_code, "云端服务暂时不可用，请稍后重试。"))
    error.status_code = response.status_code
    return error


def _llm_payload_hash(operation: str, system: str, user: str) -> str:
    request = json.dumps(
        {"operation": operation, "system": system, "user": user},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(request.encode("utf-8")).hexdigest()


def _llm_job_key(operation: str, system: str, user: str, *, attempt: int = 0) -> str:
    """Use a stable key until the relay explicitly reports job failure."""
    digest = _llm_payload_hash(operation, system, user)
    return hashlib.sha256(f"{digest}:{attempt}".encode("ascii")).hexdigest()[:32]


def _llm_job_state_path(settings, digest: str) -> Path:
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        data_dir = Path.home() / "PKU-All-in-Notion" / "data"
    return Path(data_dir) / ".llm-jobs" / f"{digest}.json"


def _llm_job_attempt(path: Path) -> int:
    try:
        state = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        raise PlatformError("无法读取笔记任务状态，请检查本机数据目录。") from exc
    attempt = state.get("attempt") if isinstance(state, dict) else None
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        raise PlatformError("笔记任务状态异常，请检查本机数据目录。")
    return attempt


def _advance_llm_job_attempt(path: Path, attempt: int) -> None:
    """Persist only a counter, never a prompt, token, or response body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(json.dumps({"attempt": attempt + 1}), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise PlatformError("无法保存笔记重试状态，请检查本机数据目录。") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _llm_notes_job(operation: str, prompt: str, settings, *, system: str, token: str) -> dict:
    endpoint = _endpoint(settings.cloud_transcribe_url, "/v1/llm/jobs")
    headers = {"Authorization": f"Bearer {token}"}
    state_path = _llm_job_state_path(settings, _llm_payload_hash(operation, system, prompt))
    attempt_number = _llm_job_attempt(state_path)
    body = {
        "operation": operation,
        "system": system,
        "user": prompt,
        "idempotency_key": _llm_job_key(operation, system, prompt, attempt=attempt_number),
    }
    configured_timeout = float(getattr(settings, "llm_timeout", _LLM_JOB_DEADLINE_SECONDS))
    deadline = time.monotonic() + min(
        _LLM_JOB_DEADLINE_SECONDS, max(1.0, configured_timeout)
    )
    # An enqueue response can be lost after the server has already accepted the
    # job. Every attempt therefore carries the same account-scoped key.
    for attempt in range(3):
        try:
            response = _post(
                endpoint, headers=headers, json=body,
                timeout=min(30, max(1.0, deadline - time.monotonic())),
            )
            break
        except httpx.TransportError as exc:
            if attempt == 2 or time.monotonic() >= deadline:
                raise PlatformError("无法连接云端服务，请稍后重试") from exc
            time.sleep(min(1 + attempt, max(0, deadline - time.monotonic())))
    if response.status_code != 202:
        raise _llm_error(response)
    try:
        accepted = response.json()
    except ValueError as exc:
        raise PlatformError("云端返回异常，请联系管理员") from exc
    job_id = accepted.get("job_id") if isinstance(accepted, dict) else None
    if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise PlatformError("云端返回异常，请联系管理员")
    job_url = f"{endpoint}/{job_id}"
    while time.monotonic() < deadline:
        try:
            result = _get(job_url, headers=headers,
                          timeout=min(20, max(1.0, deadline - time.monotonic())))
        except httpx.TransportError:
            # The server keeps the job alive independently of this connection.
            time.sleep(min(_LLM_JOB_POLL_SECONDS, max(0, deadline - time.monotonic())))
            continue
        if result.status_code != 200:
            raise _llm_error(result)
        try:
            payload = result.json()
        except ValueError as exc:
            raise PlatformError("云端返回异常，请联系管理员") from exc
        if not isinstance(payload, dict) or payload.get("job_id") != job_id:
            raise PlatformError("云端返回异常，请联系管理员")
        status = payload.get("status")
        if status == "succeeded":
            content = payload.get("content")
            points = payload.get("points_charged")
            if not isinstance(content, str) or not isinstance(points, (int, float)) or isinstance(points, bool):
                raise PlatformError("云端返回异常，请联系管理员")
            return {"content": content, "points_charged": points}
        if status == "failed":
            error_status = payload.get("error_status")
            if not isinstance(error_status, int) or isinstance(error_status, bool):
                raise PlatformError("云端返回异常，请联系管理员")
            failure = _llm_error(httpx.Response(error_status))
            # The relay error is operator text; retain only the safe mapping.
            _advance_llm_job_attempt(state_path, attempt_number)
            raise failure
        if status not in {"queued", "running"}:
            raise PlatformError("云端返回异常，请联系管理员")
        time.sleep(min(_LLM_JOB_POLL_SECONDS, max(0, deadline - time.monotonic())))
    raise PlatformError("笔记生成仍在云端进行，请稍后重新整理；不会重复扣费。")


def llm(operation: str, prompt: str, settings, *, system: str | None = None) -> dict:
    """Run one approved metered LLM operation through the relay."""
    token = (settings.platform_token or "").strip()
    if not token:
        raise PlatformError("云端登录已失效，请重新激活后重试。")
    resolved_system = system or _DEFAULT_LLM_SYSTEM
    if operation == "notes":
        return _llm_notes_job(operation, prompt, settings, system=resolved_system, token=token)
    try:
        response = _post(
            _endpoint(settings.cloud_transcribe_url, "/v1/llm"),
            headers={"Authorization": f"Bearer {token}"},
            json={"operation": operation, "system": resolved_system, "user": prompt},
            timeout=settings.llm_timeout,
        )
    except httpx.HTTPError as exc:
        raise PlatformError("无法连接云端服务，请稍后重试") from exc
    if response.status_code != 200:
        raise _llm_error(response)
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
        response = _get(
            _endpoint(settings.cloud_transcribe_url, "/v1/quota"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        return {"active": True, "available": False}
    if response.status_code == 401:
        _clear_session(settings)
        return {"active": False}
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
    # Product-account fields: pass through only when the relay provides them.
    if "email" in payload:
        result["email"] = payload.get("email")
    if "email_verified" in payload:
        result["email_verified"] = bool(payload.get("email_verified"))
    if "legacy_activate" in payload:
        result["legacy_activate"] = bool(payload.get("legacy_activate"))
    return result
