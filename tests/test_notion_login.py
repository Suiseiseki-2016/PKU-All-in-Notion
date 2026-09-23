"""Unit tests for the Notion OAuth browser login (no network, no browser)."""

from __future__ import annotations

import base64
import json as json_lib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from pku_sync import notion_login
from pku_sync.notion import NotionError

REDIRECT = "http://localhost:8765/callback"


def make_settings(**overrides) -> SimpleNamespace:
    base = dict(notion_oauth_client_id="cid-1", notion_oauth_client_secret="cs-1")
    base.update(overrides)
    return SimpleNamespace(**base)


def isolated_env(monkeypatch, tmp_path: Path) -> Path:
    """Point both env search paths at temp dirs so tests never touch the repo .env."""
    monkeypatch.chdir(tmp_path)
    pkg_root = tmp_path / "fake-pkg-root"
    pkg_root.mkdir()
    monkeypatch.setattr(notion_login, "_PACKAGE_ROOT", pkg_root)
    return pkg_root


# -- pure pieces ---------------------------------------------------------------


def test_authorize_url_params():
    url = notion_login.authorize_url("cid-1", REDIRECT, "st-9")
    assert url.startswith("https://api.notion.com/v1/oauth/authorize?")
    query = parse_qs(urlparse(url).query)
    assert query == {
        "client_id": ["cid-1"],
        "redirect_uri": [REDIRECT],
        "response_type": ["code"],
        "state": ["st-9"],
    }


def test_exchange_code_request_and_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json_lib.loads(request.read())
        return httpx.Response(
            200,
            json={"access_token": "secret_x", "bot_id": "bot-1", "workspace_name": "测试空间"},
        )

    out = notion_login.exchange_code(
        "cid-1", "cs-1", "the-code", REDIRECT, transport=httpx.MockTransport(handler)
    )
    assert out["access_token"] == "secret_x"
    assert out["workspace_name"] == "测试空间"
    assert seen["path"] == "/v1/oauth/token"
    decoded = base64.b64decode(seen["auth"].split(" ", 1)[1]).decode()
    assert decoded == "cid-1:cs-1"
    assert seen["body"] == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": REDIRECT,
    }


def test_exchange_code_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"object": "error", "code": "invalid_grant", "message": "code expired"}
        )

    with pytest.raises(NotionError, match="invalid_grant"):
        notion_login.exchange_code("cid-1", "cs-1", "bad", REDIRECT, transport=httpx.MockTransport(handler))


def test_exchange_code_missing_token_in_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workspace_name": "w"})

    with pytest.raises(NotionError, match="access_token"):
        notion_login.exchange_code("cid-1", "cs-1", "c", REDIRECT, transport=httpx.MockTransport(handler))


# -- .env writing ----------------------------------------------------------------


def test_write_env_token_appends(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PKU_USERNAME=123\nPKU_PASSWORD=p\n", encoding="utf-8")
    notion_login.write_env_token(env, "secret_a")
    text = env.read_text("utf-8")
    assert "PKU_USERNAME=123" in text and "PKU_PASSWORD=p" in text
    assert "NOTION_TOKEN=secret_a" in text
    assert text.endswith("\n")


def test_write_env_token_replaces_existing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nNOTION_TOKEN=secret_old\nB=2\n", encoding="utf-8")
    notion_login.write_env_token(env, "secret_new")
    text = env.read_text("utf-8")
    assert "secret_old" not in text
    assert text.count("NOTION_TOKEN=") == 1
    assert "A=1" in text and "B=2" in text


def test_write_env_token_keeps_commented_example(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# NOTION_TOKEN=\n", encoding="utf-8")
    notion_login.write_env_token(env, "secret_a")
    text = env.read_text("utf-8")
    assert "# NOTION_TOKEN=" in text  # the .env.example-style comment stays
    assert "NOTION_TOKEN=secret_a" in text


def test_write_env_token_creates_file(tmp_path):
    env = tmp_path / ".env"
    notion_login.write_env_token(env, "secret_a")
    assert env.read_text("utf-8") == "NOTION_TOKEN=secret_a\n"


def test_write_env_token_preserves_utf8(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NOTES_MODEL=qwen/中文模型\n", encoding="utf-8")
    notion_login.write_env_token(env, "secret_a")
    assert "中文模型" in env.read_text("utf-8")


def test_env_file_path_prefers_cwd(tmp_path, monkeypatch):
    isolated_env(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("X=1", encoding="utf-8")
    assert notion_login.env_file_path(None) == tmp_path / ".env"


def test_env_file_path_falls_back_to_package_root(tmp_path, monkeypatch):
    pkg_root = isolated_env(monkeypatch, tmp_path)
    (pkg_root / ".env").write_text("X=1", encoding="utf-8")
    assert notion_login.env_file_path(None) == pkg_root / ".env"


def test_env_file_path_creates_cwd_when_nothing_exists(tmp_path, monkeypatch):
    isolated_env(monkeypatch, tmp_path)  # neither cwd nor fake pkg root has .env
    assert notion_login.env_file_path(None) == tmp_path / ".env"


# -- full login flow (real local server, mocked token exchange) -------------------


def _run_login_thread(settings, **kwargs):
    urls: list[str] = []
    results: list = []

    def run():
        try:
            results.append(notion_login.login(settings, open_browser=False, on_url=urls.append, **kwargs))
        except Exception as exc:  # surfaced in the main thread below
            results.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    for _ in range(600):  # wait for the local server + URL (slow CI runners)
        if urls:
            break
        time.sleep(0.05)
    assert urls, "authorize URL was never produced"
    return thread, urls, results


def _callback_port(url: str) -> int:
    """The local port, from the redirect_uri param of the authorize URL."""
    redirect = parse_qs(urlparse(url).query)["redirect_uri"][0]
    return int(urlparse(redirect).netloc.rsplit(":", 1)[1])


def test_login_full_flow_with_state_rejection(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/oauth/token"
        return httpx.Response(
            200,
            json={"access_token": "secret_new", "bot_id": "bot-9", "workspace_name": "测试空间"},
        )

    thread, urls, results = _run_login_thread(
        make_settings(),
        port=0,
        timeout=15,
        state="st-abc",
        transport=httpx.MockTransport(handler),
    )
    assert urls[0].startswith("https://api.notion.com/v1/oauth/authorize?")
    assert "state=st-abc" in urls[0]
    port = _callback_port(urls[0])

    with httpx.Client(trust_env=False) as http:  # never route localhost via a proxy
        wrong = http.get(f"http://localhost:{port}/callback", params={"code": "x", "state": "nope"})
        assert wrong.status_code == 400  # stale/foreign state is rejected…
        ok = http.get(
            f"http://localhost:{port}/callback", params={"code": "goodcode", "state": "st-abc"}
        )
        assert ok.status_code == 200  # …without stopping the wait

    thread.join(10)
    assert not thread.is_alive()
    if results and isinstance(results[0], Exception):
        raise results[0]
    info = results[0]
    assert info["access_token"] == "secret_new"
    assert info["workspace_name"] == "测试空间"
    assert info["env_path"] == tmp_path / ".env"
    assert "NOTION_TOKEN=secret_new" in (tmp_path / ".env").read_text("utf-8")


def test_login_denied_callback(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    thread, urls, results = _run_login_thread(make_settings(), port=0, timeout=15, state="st-1")
    port = _callback_port(urls[0])
    with httpx.Client(trust_env=False) as http:
        denied = http.get(
            f"http://localhost:{port}/callback", params={"error": "access_denied", "state": "st-1"}
        )
        assert denied.status_code == 400
    thread.join(10)
    assert not thread.is_alive()
    assert isinstance(results[0], NotionError)
    assert "授权被拒绝" in str(results[0])
    assert not (tmp_path / ".env").exists()  # nothing written on denial


def test_login_timeout(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    with pytest.raises(NotionError, match="超时"):
        notion_login.login(make_settings(), port=0, open_browser=False, timeout=0.3, state="st-2")
    assert not (tmp_path / ".env").exists()


def test_login_requires_oauth_credentials(monkeypatch):
    with pytest.raises(NotionError, match="OAuth 客户端"):
        notion_login.login(make_settings(notion_oauth_client_id="", notion_oauth_client_secret=""))
