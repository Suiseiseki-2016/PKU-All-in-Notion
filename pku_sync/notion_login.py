"""Browser OAuth login for Notion (``pku-sync notion login``).

Multi-user credential story: every user runs ``pku-sync notion login`` once.
The browser opens Notion's authorization page, the user picks the pages to
share (their Class Notes hub), and the exchanged token lands in the local
.env as ``NOTION_TOKEN`` -- which then feeds the same REST client as a
hand-filled internal integration token. No my-integrations visit, no
copy-paste. The manual ``NOTION_TOKEN`` path stays as the fallback.

One-time developer setup: create a **public** integration (internal ones
cannot authorize other users) at notion.so/my-integrations and fill
``NOTION_OAUTH_CLIENT_ID`` / ``NOTION_OAUTH_CLIENT_SECRET``. The redirect
URI registered with the integration must match the local callback,
``http://localhost:<port>/callback`` (default port 8765; change with
--port and update the integration accordingly).

The token exchange is deliberately NOT retried: a failed exchange consumes
the single-use authorization code, so retrying the same code can never
succeed; the user simply runs login again.
"""

from __future__ import annotations

import base64
import secrets as secrets_mod
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


def verify_token(settings=None) -> str:
    """Whoami with the saved token → "name @ workspace" for display.

    Constructs a fresh Settings by default: login() just wrote the token to
    .env, and the module-level cached instance was created before that.
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
    """Run the whole browser flow; returns token info plus where it was saved.

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
    if "error" in server.result:
        raise NotionError(f"授权被拒绝：{server.result['error']}")
    if "code" not in server.result:
        raise NotionError(
            f"等待浏览器授权超时（>{timeout:.0f}s）。重跑一次 `pku-sync notion login` 即可。"
        )
    token_info = exchange_code(
        client_id, client_secret, server.result["code"], redirect_uri, transport=transport
    )
    env_path = write_env_token(env_file_path(settings), token_info["access_token"])
    return {**token_info, "env_path": env_path, "authorize_url": url}
