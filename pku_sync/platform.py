"""Local-only bridge between the student panel and the operated relay."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .envfile import write_env_values
from .notion_login import env_file_path

DEFAULT_TRANSCRIBE_URL = "https://pku.aeoluswu.info/v1/transcribe"
_request_client = ContextVar('platform_task_request_client', default=None)
_read_only_worker = ContextVar('platform_read_only_preparation_worker', default=False)
_read_worker_stop = ContextVar('platform_read_only_worker_stop', default=None)


@contextmanager
def request_scope():
    """Reuse connections only for this task; retain each request's credentials."""
    if _request_client.get() is not None:
        yield
        return
    with httpx.Client(follow_redirects=False) as client:
        token = _request_client.set(client)
        try:
            yield
        finally:
            _request_client.reset(token)


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """Share the caller's read budget; never implicitly replay uncertain writes."""
    if _read_only_worker.get() and method != 'GET':
        raise PlatformError('课程准备线程只读，禁止发出修改请求。')
    stopped = _read_worker_stop.get()
    if stopped is not None and stopped.is_set():
        raise PlatformError('课程准备读取已取消。')
    sender = {"GET": httpx.get, "POST": httpx.post, "PUT": httpx.put}[method]
    timeout = kwargs.get('timeout', 5.0)
    budget = timeout.read if isinstance(timeout, httpx.Timeout) else timeout
    deadline = time.monotonic() + budget if method == 'GET' and budget is not None else None
    try:
        client = _request_client.get()
        return client.request(method, url, **kwargs) if client is not None else sender(url, **kwargs)
    except httpx.TransportError:
        # A response can be lost after a write has already taken effect. A
        # transport exception does not establish that POST/PUT was rejected.
        # Business protocols with durable idempotency keys perform their own
        # explicit recovery; the generic transport must never replay writes.
        if method != "GET":
            raise
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            if isinstance(timeout, httpx.Timeout):
                kwargs['timeout'] = httpx.Timeout(**{
                    phase: min(value, remaining) if value is not None else None
                    for phase, value in timeout.as_dict().items()})
            else:
                kwargs['timeout'] = remaining
        # A configured proxy can be alive while still breaking TLS for this
        # relay. Retry the same HTTPS read directly before surfacing an
        # offline state, using only the time left by the first read;
        # authentication remains protected by TLS either way.
        with httpx.Client(trust_env=False) as client:
            return client.request(method, url, **kwargs)


def _post(url: str, **kwargs) -> httpx.Response:
    return _request("POST", url, **kwargs)


def _get(url: str, **kwargs) -> httpx.Response:
    return _request("GET", url, **kwargs)


def _put(url: str, **kwargs) -> httpx.Response:
    return _request("PUT", url, **kwargs)


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


def _extract_code_from_input(raw: str) -> str:
    """Extract a gift token, invite code, or redeem code from a full URL or raw string."""
    text = (raw or "").strip()
    if not text:
        return ""
    # Match /gift/<token> or /invite/<code> anywhere in a pasted URL or path
    match = re.search(r"/(?:gift|invite|redeem)/([A-Za-z0-9_-]+)", text)
    if match:
        return match.group(1)
    # Match ?gift=... or ?invite=... or ?code=...
    qmatch = re.search(r"[?&](?:gift|invite|code)=([A-Za-z0-9_-]+)", text)
    if qmatch:
        return qmatch.group(1)
    return text


_OPTIONAL_ACCOUNT_KEYS = (
    "tier",
    "tier_expires_at",
    "basic_seconds_remaining",
    "advanced_seconds_remaining",
    "weekly_rollover_seconds",
    "next_weekly_refill_at",
    "invite_code",
    "invite_url",
    "invited_count",
    "invited_by",
    "campus_bound",
    "campus_id_masked",
    "invite_reward_pending",
    "invite_pending_campus_reward",
)


def _copy_optional_account_fields(payload: dict, result: dict, settings=None) -> None:
    for key in _OPTIONAL_ACCOUNT_KEYS:
        if key in payload and payload.get(key) is not None:
            result[key] = payload.get(key)
    if settings is not None and result.get("invite_code") and not result.get("invite_url"):
        result["invite_url"] = _endpoint(settings.cloud_transcribe_url, "/invite/" + result["invite_code"])


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
    result = {
        "email": email.strip().lower(),
        "email_verified": verified,
        "active": True,
    }
    _copy_optional_account_fields(payload, result, settings)
    return result


def _profile_headers(settings) -> dict[str, str] | None:
    token = (getattr(settings, "platform_token", "") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else None


def _profile_endpoint(settings) -> str | None:
    configured = getattr(settings, "cloud_transcribe_url", None)
    if not isinstance(configured, str):
        return None
    return _endpoint(configured, "/v1/profile")








def _fetch_account_profile(settings) -> dict:
    headers = _profile_headers(settings)
    if headers is None:
        return {"available": False, "status": "signed_out"}
    url = _profile_endpoint(settings)
    if url is None:
        return {"available": False, "status": "client_unconfigured"}
    try:
        response = _get(
            url,
            headers=headers,
            timeout=15,
        )
    except httpx.HTTPError:
        return {"available": False, "status": "offline"}
    if response.status_code == 403:
        return {"available": True, "status": "email_verification_required"}
    if response.status_code in {404, 501, 503}:
        return {"available": False, "status": "server_upgrade_required"}
    if response.status_code != 200:
        return {"available": False, "status": "unavailable"}
    try:
        payload = response.json()
    except ValueError:
        return {"available": False, "status": "invalid_response"}
    revision = payload.get("revision") if isinstance(payload, dict) else None
    profile = payload.get("profile") if isinstance(payload, dict) else None
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
        or not isinstance(profile, dict)
    ):
        return {"available": False, "status": "invalid_response"}
    return {"available": True, "status": "ready", "revision": revision, "profile": profile}


def _put_account_profile(settings, revision: int, profile: dict, *, replace_workspace_id=None) -> dict:
    headers = _profile_headers(settings)
    if headers is None:
        return {"available": False, "status": "signed_out"}
    url = _profile_endpoint(settings)
    if url is None:
        return {"available": False, "status": "client_unconfigured"}
    body = {"revision": revision, "profile": profile}
    if replace_workspace_id is not None:
        body['replace_workspace_id'] = replace_workspace_id
    try:
        response = _put(
            url,
            headers=headers,
            json=body,
            timeout=15,
        )
    except httpx.HTTPError:
        return {"available": False, "status": "offline"}
    if response.status_code == 409:
        return {"available": True, "status": "conflict"}
    if response.status_code != 200:
        return {"available": False, "status": "unavailable"}
    try:
        payload = response.json()
    except ValueError:
        return {"available": False, "status": "invalid_response"}
    revision = payload.get("revision") if isinstance(payload, dict) else None
    return {
        "available": True,
        "status": "pushed",
        "revision": revision if isinstance(revision, int) else None,
    }


def sync_account_profile(settings) -> dict:
    """Reconcile safe account settings without ever uploading credentials or content."""
    remote = _fetch_account_profile(settings)
    if remote.get("status") != "ready":
        return remote
    revision = remote["revision"]
    profile = remote["profile"]
    if profile.get("notion_workspace_v2"):
        from .panel.workspace_profile import pull
        workspace_result = pull(settings, remote)
        if workspace_result.get("status") != "pulled":
            return {"available": True, "status": workspace_result["status"], "revision": revision}
        return {'available': True, 'status': 'pulled', 'revision': revision}
    return {"available": True, "status": "current", "revision": revision}


def activate(code: str, settings) -> dict:
    """Redeem one code and keep the returned session token in local .env only.

    Legacy pilot path: the code creates an anonymous session. Product accounts
    should use ``redeem`` after email+password login instead.
    """
    clean_code = _extract_code_from_input(code)
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


def register(email: str, password: str, settings, *, invite_code: str = "") -> dict:
    """Create a product account; session token is stored locally like activate."""
    url = _endpoint(settings.cloud_transcribe_url, "/v1/auth/register")
    body: dict[str, str] = {"email": email, "password": password}
    clean_invite = _extract_code_from_input(invite_code)
    if clean_invite:
        body["invite_code"] = clean_invite
    try:
        response = _post(
            url, json=body, timeout=30
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
    _copy_optional_account_fields(payload, result, settings)
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
    """Apply a gift link, invite code, or recharge code to the logged-in product account."""
    clean_code = _extract_code_from_input(code)
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
            409: "该邀请码或礼物链接已绑定过",
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
    result = {"transcribe_seconds_remaining": seconds}
    if payload.get("llm_points_remaining") is not None:
        result["llm_points_remaining"] = payload.get("llm_points_remaining")
    _copy_optional_account_fields(payload, result, settings)
    for extra in ("kind", "reward_summary", "invite_bound"):
        if extra in payload and payload.get(extra) is not None:
            result[extra] = payload.get(extra)
    return result


def exchange_quota(hours: int, settings, *, target: str = "advanced") -> dict:
    """Exchange AI points for transcription hours (10 AI points = 1 hour)."""
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 100:
        raise PlatformError("兑换小时数必须是正整数")
    clean_target = (target or "advanced").strip().lower()
    if clean_target not in {"basic", "advanced"}:
        raise PlatformError("请选择兑换普通转写或高级转写时长")
    token = (settings.platform_token or "").strip()
    if not token:
        raise PlatformError("请先登录后再兑换时长")
    try:
        response = _post(
            _endpoint(settings.cloud_transcribe_url, "/v1/quota/exchange"),
            headers={"Authorization": f"Bearer {token}"},
            json={"hours": hours, "target": clean_target},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PlatformError("无法连接云端服务，请稍后重试") from exc
    if response.status_code != 200:
        detail = _relay_detail(response, "")
        messages = {
            400: detail or "兑换参数无效",
            401: "登录已过期，请重新登录后操作",
            402: detail or "AI 点不足（10 AI 点可兑换 1 小时转写时长）",
            403: "请先完成邮箱验证后再兑换额度",
        }
        error = PlatformError(messages.get(response.status_code, detail or "兑换没有成功，请稍后重试"))
        error.status_code = response.status_code
        raise error
    payload = response.json()
    seconds = payload.get("transcribe_seconds_remaining")
    if not isinstance(seconds, int):
        raise PlatformError("云端返回异常，请联系管理员")
    result = {"transcribe_seconds_remaining": seconds}
    if payload.get("llm_points_remaining") is not None:
        result["llm_points_remaining"] = payload.get("llm_points_remaining")
    _copy_optional_account_fields(payload, result, settings)
    for extra in ("points_spent", "seconds_added", "target"):
        if extra in payload and payload.get(extra) is not None:
            result[extra] = payload.get(extra)
    return result


def bind_campus(student_id: str, settings) -> dict:
    """Bind a verified PKU student ID to the current account to unlock invite rewards."""
    clean_id = (student_id or "").strip()
    if not clean_id:
        raise PlatformError("请输入教学网账号（学号）")
    token = (settings.platform_token or "").strip()
    if not token:
        return {"bound": False, "reason": "not_signed_in"}
    try:
        response = _post(
            _endpoint(settings.cloud_transcribe_url, "/v1/auth/bind-campus"),
            headers={"Authorization": f"Bearer {token}"},
            json={"student_id": clean_id},
            timeout=15,
        )
    except httpx.HTTPError:
        return {"bound": False, "reason": "offline"}
    if response.status_code != 200:
        return {"bound": False, "status_code": response.status_code, "detail": _relay_detail(response, "")}
    try:
        payload = response.json()
    except ValueError:
        return {"bound": False, "reason": "invalid_response"}
    if not isinstance(payload, dict):
        return {"bound": False, "reason": "invalid_response"}
    result = {"bound": bool(payload.get("campus_bound", False)), "reward_granted": bool(payload.get("reward_granted", False))}
    if payload.get("transcribe_seconds_remaining") is not None:
        result["transcribe_seconds_remaining"] = payload.get("transcribe_seconds_remaining")
    if payload.get("llm_points_remaining") is not None:
        result["llm_points_remaining"] = payload.get("llm_points_remaining")
    _copy_optional_account_fields(payload, result, settings)
    return result


_DEFAULT_LLM_SYSTEM = "你是课程练习整理助手。只返回请求指定的 JSON。"
_LLM_JOB_POLL_SECONDS = 2.5
_LLM_JOB_DEADLINE_SECONDS = 12 * 60


def _llm_error(response: httpx.Response) -> PlatformError:
    if response.status_code == 403 and "AI model not enabled" in _relay_detail(response, ""):
        error = PlatformError("当前账号尚未开通所选 AI 模型，请切换模型或联系管理员。")
        error.status_code = 403
        return error
    safe = {
        401: "云端登录已失效，请重新激活后重试。",
        402: "AI 点不足，请兑换新额度后重试。",
        403: "请先完成邮箱验证后再使用转写、AI 或兑换额度",
        409: "任务源身份与已有记录不一致，已停止重复调用；请核对原任务及费用记录。",
        413: "AI 请求超过云端传输容量，任务未提交；原资料与已完成响应保留。",
        502: "AI 服务暂时没有完成请求，请稍后重试。",
        503: "AI 服务暂未配置，请联系管理员后重试。",
    }
    error = PlatformError(safe.get(response.status_code, "云端服务暂时不可用，请稍后重试。"))
    error.status_code = response.status_code
    return error


# The deployed relay's default HTTP body allowance. Count the real UTF-8
# JSON envelope, including an escaped prompt and operation metadata.
LLM_REQUEST_MAX_BYTES = 250 * 1024
SELECTABLE_NOTE_MODELS = frozenset({"qwen3.5-flash", "qwen3.7-plus", "qwen3.8-max-0902"})


def llm_request_body_size(operation, system, prompt, *, model=None, operation_context=None):
    model = "qwen3.8-max-0902" if model == "qwen3.8-max" else model
    body = {"operation": operation, "system": system, "user": prompt,
            "idempotency_key": "0" * 32}
    if model is not None:
        body["model"] = model
    if operation_context:
        body["operation_context"] = operation_context
    return len(httpx.Request("POST", "https://unused.invalid/v1/llm/jobs", json=body).content)


def _llm_payload_hash(operation: str, system: str, user: str, *, model: str | None = None) -> str:
    payload = {"operation": operation, "system": system, "user": user}
    if operation == "notes" and model is not None:
        payload["model"] = model
    request = json.dumps(
        payload,
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(request.encode("utf-8")).hexdigest()


def _llm_job_key(operation: str, system: str, user: str, *, attempt: int = 0,
                 model: str | None = None) -> str:
    """Use a stable key until the relay explicitly reports job failure."""
    digest = _llm_payload_hash(operation, system, user, model=model)
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


def _llm_durable_job(operation: str, prompt: str, settings, *, system: str, token: str, operation_context=None) -> dict:
    selected_model = None
    if operation == "notes":
        requested_model = getattr(settings, "notes_model", None)
        if requested_model == "qwen3.8-max":
            requested_model = "qwen3.8-max-0902"
        if requested_model in SELECTABLE_NOTE_MODELS:
            selected_model = requested_model
        elif requested_model not in (None, "", "qwen/qwen3.5-397b-a17b"):
            raise PlatformError("云端笔记模型无效；请明确选择服务支持的模型，原稿保留。")
    if len(system) + len(prompt) > 200000:
        raise PlatformError("云端模型完整输入超过 200000 字符；来源未裁剪，任务未提交。")
    if llm_request_body_size(operation, system, prompt, model=selected_model,
                             operation_context=operation_context) > LLM_REQUEST_MAX_BYTES:
        raise _llm_error(httpx.Response(413))
    endpoint = _endpoint(settings.cloud_transcribe_url, "/v1/llm/jobs")
    headers = {"Authorization": f"Bearer {token}"}
    state_path = _llm_job_state_path(settings, _llm_payload_hash(operation, system, prompt,
                                                            model=selected_model))
    # Quiz/grade keys remain identical across devices and restarts. In
    # particular an expired result or ambiguous settlement must not advance
    # to a fresh billable request on just one device.
    attempt_number = _llm_job_attempt(state_path) if operation == "notes" else 0
    body = {
        "operation": operation,
        "system": system,
        "user": prompt,
        "idempotency_key": _llm_job_key(operation, system, prompt, attempt=attempt_number,
                                         model=selected_model),
    }
    if selected_model is not None:
        body["model"] = selected_model
    if operation_context:
        body['operation_context'] = operation_context
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
            if operation == "notes":
                _advance_llm_job_attempt(state_path, attempt_number)
            raise failure
        if status not in {"queued", "running"}:
            raise PlatformError("云端返回异常，请联系管理员")
        time.sleep(min(_LLM_JOB_POLL_SECONDS, max(0, deadline - time.monotonic())))
    raise PlatformError("AI 任务仍在云端进行，请稍后重试；相同请求不会重复扣费。")


def llm(operation: str, prompt: str, settings, *, system: str | None = None, operation_context=None) -> dict:
    """Run one approved metered LLM operation through the relay."""
    token = (settings.platform_token or "").strip()
    if not token:
        raise PlatformError("云端登录已失效，请重新激活后重试。")
    resolved_system = system or _DEFAULT_LLM_SYSTEM
    context = operation_context if operation_context is not None else getattr(settings, '_metered_operation_context', {})
    if (not isinstance(context, dict) or set(context) - {'purpose', 'course_id', 'content_id', 'source_id', 'client_operation_id'}
            or any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_:.-]{1,160}', value)
                   for value in context.values())):
        raise PlatformError('付费任务源身份无效，已停止发送。')
    if operation in {"notes", "quiz", "grade", "qa"}:
        return _llm_durable_job(operation, prompt, settings, system=resolved_system, token=token,
                                operation_context=context)
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
    _copy_optional_account_fields(payload, result, settings)
    return result
