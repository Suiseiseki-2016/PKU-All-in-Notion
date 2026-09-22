"""Browser OAuth login for Notion (``pku-sync notion login``).

Multi-user credential story: every user runs ``pku-sync notion login`` once
after activating their platform account. The browser opens Notion's
authorization page, the user picks the pages to share (their Class Notes
hub), and the exchanged token lands in the local .env as ``NOTION_TOKEN``
-- which then feeds the same REST client as a hand-filled internal
integration token. No my-integrations visit, no copy-paste. The manual
``NOTION_TOKEN`` path stays as the fallback.

The authorization-code exchange goes through the PLATFORM RELAY
(``POST /v1/notion/exchange``) with the local ``PLATFORM_TOKEN`` as the
Bearer credential: the client sends only ``{code, redirect_uri}`` and the
server-held Notion OAuth client secret never touches this machine. Relay
failures are genericized locally -- the authorization code and any upstream
response body never appear in client copy.

One-time developer setup: create a **public** integration (internal ones
cannot authorize other users) at notion.so/my-integrations and fill
``NOTION_OAUTH_CLIENT_ID`` (the public client id used to build the authorize
URL; the secret stays server-side). Register BOTH callback redirect URIs on
the integration: ``http://localhost:8765/callback`` and
``http://localhost:8766/callback`` -- the loopback listener picks the first
free approved port and never selects an arbitrary one.

The token exchange is deliberately NOT retried: a failed exchange consumes
the single-use authorization code, so retrying the same code can never
succeed; the user simply runs login again (a fresh authorization).

The legacy self-hosted REST fallback (``resolve_client`` / ``exchange_code``
/ ``login``) is kept for advanced users running their own relay-less setup;
it is no longer the ``pku-sync notion login`` path.
"""

from __future__ import annotations

import base64
import secrets as secrets_mod
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .envfile import write_env_values
from .notion import API_BASE, NotionError

DEFAULT_PORT = 8765
CALLBACK_PATH = "/callback"

# The only loopback ports the OAuth callback may ever bind -- exactly the
# redirect URIs allowlisted on the relay. The listener tries the preferred
# port first and falls back to the other one; an arbitrary port is never
# selected, and a process occupying a port is never killed.
CALLBACK_PORTS = (8765, 8766)

# Pinned, actionable copy when BOTH approved callback ports are occupied.
# A named constant (not an inline literal) so the launcher and its test
# share the exact string: it names both approved ports and the recovery
# action.
CALLBACK_PORTS_BUSY_MESSAGE = "本地回调端口 8765 与 8766 都被占用：关闭占用端口的应用后重试。"

def resolve_client(settings) -> tuple[str, str]:
    """OAuth client credentials for the optional self-hosted REST fallback."""
    client_id = (getattr(settings, "notion_oauth_client_id", "") or "").strip()
    client_secret = (getattr(settings, "notion_oauth_client_secret", "") or "").strip()
    if not client_id or not client_secret:
        raise NotionError(
            "REST fallback 没有 OAuth 客户端：请在 .env 设置 "
            "NOTION_OAUTH_CLIENT_ID/SECRET；普通用户请改用官方 Notion MCP。"
        )
    return client_id, client_secret


def resolve_client_id(settings) -> str:
    """The PUBLIC OAuth client id used to build the authorize URL.

    Only the public client id is needed client-side: the secret stays on the
    relay, so unlike the legacy ``resolve_client`` this never requires
    ``NOTION_OAUTH_CLIENT_SECRET``.
    """
    client_id = (getattr(settings, "notion_oauth_client_id", "") or "").strip()
    if not client_id:
        raise NotionError(
            "缺少 NOTION_OAUTH_CLIENT_ID：请在 .env 填入集成的公开 client id"
            "（client secret 由平台服务端持有，本地无需配置）。"
        )
    return client_id


def resolve_platform_token(settings) -> str:
    """The local platform session token used as the relay Bearer credential."""
    token = (getattr(settings, "platform_token", "") or "").strip()
    if not token:
        raise NotionError(
            "尚未激活平台账号：请先在面板完成兑换码激活（写入 PLATFORM_TOKEN），"
            "再运行 Notion 登录。"
        )
    return token


def relay_exchange_url(settings) -> str:
    """``/v1/notion/exchange`` on the same relay base as transcription."""
    from .platform import _endpoint  # local import: platform imports this module

    return _endpoint(
        getattr(settings, "cloud_transcribe_url", "") or None, "/v1/notion/exchange"
    )


def verify_token(settings=None) -> str:
    """Whoami with the saved token → "name @ workspace" for display.

    Constructs a fresh Settings by default: the login flow just wrote the
    token to .env, and the module-level cached instance was created before
    that.
    """
    if settings is None:
        from .config import Settings

        settings = Settings()
    from .notion import get_client

    with get_client(settings) as client:
        me = client.whoami()
    owner = (me.get("bot") or {}).get("owner") or {}
    return f"{me.get('name') or '(未命名集成)'} @ {owner.get('workspace_name') or 'workspace'}"

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """The Notion page where the user grants (or denies) access."""
    return f"{API_BASE}/oauth/authorize?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """One-shot authorization-code → access-token exchange (no retry)."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    with httpx.Client(transport=transport, timeout=30.0) as http:
        response = http.post(
            f"{API_BASE}/oauth/token",
            headers={"Authorization": f"Basic {basic}"},
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    if response.status_code != 200:
        try:
            body = response.json()
            detail = f"{body.get('code', '')} {body.get('message', '')}".strip()
        except ValueError:
            detail = (response.text or "")[:200]
        raise NotionError(
            f"token 交换失败（HTTP {response.status_code}）{detail}".strip(),
            status=response.status_code,
        )
    payload = response.json()
    if not payload.get("access_token"):
        raise NotionError("token 交换响应里没有 access_token")
    return payload


# Generic relay-failure copy: status-specific, but never interpolating the
# authorization code, the redirect URI, or any relay/provider response body.
_RELAY_ERROR_MESSAGES = {
    400: "授权交换请求被拒绝：请重新运行一次 `pku-sync notion login`。",
    401: "平台登录状态已失效：请重新激活后再登录 Notion。",
    402: "平台配额不足：无法完成授权交换。",
    429: "请求过于频繁：请稍等片刻后重新运行登录。",
    502: "Notion 授权交换失败：请重新运行一次登录（原授权码已失效）。",
    503: "Notion 授权服务暂不可用：请稍后重试。",
}


def relay_exchange_code(
    platform_token: str,
    url: str,
    code: str,
    redirect_uri: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """One-shot authorization-code exchange through the platform relay.

    Sends ``{code, redirect_uri}`` with ``Authorization: Bearer <platform_token>``;
    the Notion client secret lives on the relay, never here. Never retried:
    a failed exchange consumes the single-use code (retry = a fresh login).
    Errors are generic -- the message contains neither the authorization code
    nor any relay/provider response body.
    """
    try:
        with httpx.Client(transport=transport, timeout=30.0) as http:
            response = http.post(
                url,
                headers={"Authorization": f"Bearer {platform_token}"},
                json={"code": code, "redirect_uri": redirect_uri},
            )
    except httpx.HTTPError as exc:
        raise NotionError("无法连接云端服务：请检查网络后重试。") from exc
    if response.status_code != 200:
        message = _RELAY_ERROR_MESSAGES.get(
            response.status_code,
            f"授权交换失败（HTTP {response.status_code}）：请重新运行一次登录。",
        )
        raise NotionError(message, status=response.status_code)
    try:
        payload = response.json()
    except ValueError as exc:
        raise NotionError("云端返回异常：请重新运行一次登录。") from exc
    if not payload.get("access_token"):
        raise NotionError("授权交换响应里没有 access_token：请重新运行一次登录。")
    return payload


def env_file_path(settings) -> Path:
    """Same dual-path preference as config: CWD first, then the package root."""
    cwd_env = Path(".env").resolve()
    pkg_env = _PACKAGE_ROOT / ".env"
    if cwd_env.exists():
        return cwd_env
    if pkg_env.exists():
        return pkg_env
    return cwd_env


def write_env_token(path: Path, token: str) -> Path:
    """Set ``NOTION_TOKEN=…`` in the .env, preserving every other line."""
    return write_env_values(path, {"NOTION_TOKEN": token})


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches Notion's redirect, hands the code to the waiting login()."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self._respond(404, "不是回调路径，忽略。")
            return
        query = parse_qs(parsed.query)
        error = (query.get("error") or [""])[0]
        if error:
            self.server.result["error"] = error
            self._respond(400, f"授权失败：{error}。可关闭本页面，回到 PKU All in Notion 重试。")
            return
        state = (query.get("state") or [""])[0]
        if state != self.server.state:
            # A stale tab or a stray request: reject but keep waiting for
            # the real callback from the page this login opened.
            self._respond(400, "state 不匹配：请关闭旧授权页，用最新打开的授权页完成授权。")
            return
        code = (query.get("code") or [""])[0]
        if not code:
            self._respond(400, "回调缺少 code 参数。")
            return
        self.server.result["code"] = code
        self._respond(200, "授权成功，可以关闭本页面，回到 PKU All in Notion 继续。")

    def _respond(self, status: int, message: str) -> None:
        body = (
            "<html><head><meta charset='utf-8'><title>PKU All in Notion 授权</title></head>"
            f"<body style='font-family:sans-serif;padding:2em;line-height:1.6'>"
            f"<h3>{message}</h3></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # keep the terminal clean
        return


class _CallbackServer(ThreadingHTTPServer):
    def __init__(self, port: int, state: str):
        self.state = state
        self.result: dict = {}
        super().__init__(("127.0.0.1", port), _CallbackHandler)

    def server_bind(self):
        # http.server defaults to SO_REUSEADDR, which on Windows silently
        # allows a second bind onto an occupied port ("port hijacking") and
        # would defeat the 8765→8766 fallback: an occupied port must raise
        # OSError. Bind exclusively on Windows so this listener is an honest
        # occupant too; other platforms keep the standard SO_REUSEADDR
        # semantics (TIME_WAIT-only reuse). SO_EXCLUSIVEADDRUSE exists only
        # on Windows sockets, so the capability check keeps this branch
        # cross-platform without naming an OS.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _wait_for_callback(server, url, *, open_browser, timeout, on_url) -> dict:
    """Serve the callback until a result arrives or the timeout elapses."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if on_url:
            on_url(url)
        if open_browser:
            webbrowser.open(url)
        deadline = time.monotonic() + timeout
        while not server.result and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return server.result


def _authorization_code(result: dict, timeout: float) -> str:
    """Map the callback wait outcome to the code, or raise the pinned state."""
    if "error" in result:
        raise NotionError(f"授权被拒绝：{result['error']}")
    if "code" not in result:
        raise NotionError(
            f"等待浏览器授权超时（>{timeout:.0f}s）。重跑一次 `pku-sync notion login` 即可。"
        )
    return result["code"]


def login(
    settings,
    *,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    timeout: float = 300.0,
    state: str | None = None,
    transport: httpx.BaseTransport | None = None,
    on_url: Callable[[str], None] | None = None,
) -> dict:
    """Legacy self-hosted REST flow (local client secret); not the CLI path.

    Kept for advanced users running their own relay-less setup.
    ``state`` and ``transport`` exist for the mock tests. ``on_url`` receives
    the authorize URL right before the wait starts (the CLI prints it so the
    flow also works when no browser can be opened, e.g. over SSH).
    """
    client_id, client_secret = resolve_client(settings)
    state = state or secrets_mod.token_hex(16)
    try:
        server = _CallbackServer(port, state)
    except OSError as exc:
        raise NotionError(
            f"本地回调端口 {port} 起不来（{exc}）。用 --port 换一个端口，"
            "并同步修改集成设置里的 redirect URI。"
        ) from exc
    redirect_uri = f"http://localhost:{server.server_address[1]}{CALLBACK_PATH}"
    url = authorize_url(client_id, redirect_uri, state)
    result = _wait_for_callback(
        server, url, open_browser=open_browser, timeout=timeout, on_url=on_url
    )
    code = _authorization_code(result, timeout)
    token_info = exchange_code(
        client_id, client_secret, code, redirect_uri, transport=transport
    )
    env_path = write_env_token(env_file_path(settings), token_info["access_token"])
    return {**token_info, "env_path": env_path, "authorize_url": url}


def login_via_relay(
    settings,
    *,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    timeout: float = 300.0,
    state: str | None = None,
    transport: httpx.BaseTransport | None = None,
    on_url: Callable[[str], None] | None = None,
) -> dict:
    """Run the relay-mediated browser flow; returns token info plus where it was saved.

    The authorize URL uses only the PUBLIC client id; the authorization code
    goes to the relay's ``/v1/notion/exchange`` with the local platform token
    as the Bearer credential (no client-side OAuth secret participates). The
    callback binds loopback only and falls back within the approved pair
    (8765 → 8766); when both are occupied the pinned actionable error is
    raised -- an arbitrary port is never selected, and occupants are never
    killed. ``state`` and ``transport`` exist for the mock tests; ``on_url``
    receives the authorize URL right before the wait starts.
    """
    client_id = resolve_client_id(settings)
    platform_token = resolve_platform_token(settings)
    if port not in CALLBACK_PORTS:
        raise NotionError(
            f"回调端口仅支持 8765 或 8766（不支持 {port}）：请使用默认端口重试。"
        )
    state = state or secrets_mod.token_hex(16)
    server = None
    last_error: OSError | None = None
    for candidate in (port, next(p for p in CALLBACK_PORTS if p != port)):
        try:
            server = _CallbackServer(candidate, state)
            break
        except OSError as exc:  # occupied: try the other approved port
            last_error = exc
    if server is None:
        raise NotionError(CALLBACK_PORTS_BUSY_MESSAGE) from last_error
    bound_port = server.server_address[1]
    redirect_uri = f"http://localhost:{bound_port}{CALLBACK_PATH}"
    url = authorize_url(client_id, redirect_uri, state)
    result = _wait_for_callback(
        server, url, open_browser=open_browser, timeout=timeout, on_url=on_url
    )
    code = _authorization_code(result, timeout)
    token_info = relay_exchange_code(
        platform_token, relay_exchange_url(settings), code, redirect_uri, transport=transport
    )
    env_path = write_env_token(env_file_path(settings), token_info["access_token"])
    return {
        **token_info,
        "env_path": env_path,
        "authorize_url": url,
        "callback_port": bound_port,
    }
