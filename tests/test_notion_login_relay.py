"""Relay-mediated Notion OAuth: the `pku-sync notion login` exchange path.

New additive coverage for the M2 relay migration (VAL-SECU-020..023, 026):
the authorize code goes to the relay ``/v1/notion/exchange`` with the local
platform token as Bearer (no client-side OAuth secret participates), the
token lands only in the gitignored ``.env``, the loopback callback falls
back 8765 → 8766 with a pinned both-occupied error, client errors are
genericized, and the old login states (whoami verification, denial,
timeout, state-mismatch, one-shot exchange) survive through the relay path.

All Notion/relay legs run through ``httpx.MockTransport``; every occupancy
fixture is a validator-owned loopback listener with a liveness proof. No
real Notion consent is spent here (that is e2e-worker territory).
"""

from __future__ import annotations

import json as json_lib
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from pku_sync import notion_login
from pku_sync.notion import NotionError
from pku_sync.notion_login import CALLBACK_PORTS, CALLBACK_PORTS_BUSY_MESSAGE

REPO_ROOT = Path(__file__).resolve().parent.parent
RELAY_BASE = "https://relay.example"
RELAY_EXCHANGE_URL = f"{RELAY_BASE}/v1/notion/exchange"


# -- fixtures and helpers (all local; never imported from pre-existing files) ---


def make_settings(**overrides) -> SimpleNamespace:
    """Settings for the relay path; the client secret is a seeded honeypot."""
    base = dict(
        notion_oauth_client_id="cid-1",
        notion_oauth_client_secret="legacy-secret-honeypot",
        platform_token="platform-token-1",
        cloud_transcribe_url=f"{RELAY_BASE}/v1/transcribe",
    )
    base.update(overrides)
    if base.get("notion_oauth_client_secret") is None:
        del base["notion_oauth_client_secret"]  # attribute absent entirely
    return SimpleNamespace(**base)


def isolated_env(monkeypatch, tmp_path: Path) -> Path:
    """Point both env search paths at temp dirs so tests never touch the repo .env."""
    monkeypatch.chdir(tmp_path)
    pkg_root = tmp_path / "fake-pkg-root"
    pkg_root.mkdir()
    monkeypatch.setattr(notion_login, "_PACKAGE_ROOT", pkg_root)
    return pkg_root


def require_free(port: int) -> None:
    """Skip honestly when a foreign process already holds an approved port."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        # posix: allow binding over TIME_WAIT remnants of earlier tests in
        # this session; a LIVE foreign listeners still refuse the bind.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        pytest.skip(f"port {port} is already occupied by a foreign process")
    finally:
        probe.close()


def occupy_port(port: int) -> socket.socket:
    """A validator-owned loopback listener: a REAL occupant for the fallback."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        # Windows SO_REUSEADDR would let a second bind "hijack" the port;
        # exclusive use makes this dummy a genuine, unstealable occupant.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        # posix: TIME_WAIT-only reuse so this dummy binds despite remnants of
        # earlier tests; a second LIVE bind is still refused (that needs
        # SO_REUSEPORT), so the fallback it stands in for stays honest.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(8)
    return sock


def assert_occupant_alive(sock: socket.socket, port: int) -> None:
    """Liveness proof: the dummy still owns and serves its port."""
    probe = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn, _ = sock.accept()
    conn.close()
    probe.close()


def relay_token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "secret_relay",
            "bot_id": "bot-9",
            "workspace_name": "测试空间",
        },
    )


def _run_login_thread(settings, **kwargs):
    """login_via_relay in a thread, with the authorize URL collected via on_url."""
    urls: list[str] = []
    results: list = []

    def run():
        try:
            results.append(
                notion_login.login_via_relay(
                    settings, open_browser=False, on_url=urls.append, **kwargs
                )
            )
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


def _state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def _finish_flow(thread, urls, results) -> dict:
    thread.join(8)
    assert not thread.is_alive()
    if results and isinstance(results[0], Exception):
        raise results[0]
    assert results, "login thread produced no result"
    return results[0]


def _deliver_callback(port: int, params: dict) -> httpx.Response:
    with httpx.Client(trust_env=False) as http:  # never route localhost via a proxy
        return http.get(f"http://localhost:{port}/callback", params=params)


# -- VAL-SECU-020: exchange goes through the relay, not a local secret ----------


def test_relay_exchange_code_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json_lib.loads(request.read())
        return relay_token_response()

    out = notion_login.relay_exchange_code(
        "platform-token-1",
        RELAY_EXCHANGE_URL,
        "code-xyz",
        "http://localhost:8766/callback",
        transport=httpx.MockTransport(handler),
    )
    assert out["access_token"] == "secret_relay"
    assert out["workspace_name"] == "测试空间"
    assert seen["url"] == RELAY_EXCHANGE_URL
    assert seen["auth"] == "Bearer platform-token-1"
    # exactly {code, redirect_uri}: no grant_type, no client_id/secret in the body
    assert seen["body"] == {
        "code": "code-xyz",
        "redirect_uri": "http://localhost:8766/callback",
    }


def test_login_via_relay_exchange_request_shape(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    require_free(8765)
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return relay_token_response()

    thread, urls, results = _run_login_thread(
        make_settings(),
        port=8765,
        timeout=5,
        state="st-relay-1",
        transport=httpx.MockTransport(handler),
    )
    assert urls[0].startswith("https://api.notion.com/v1/oauth/authorize?")
    assert _callback_port(urls[0]) == 8765
    ok = _deliver_callback(8765, {"code": "code-abc", "state": "st-relay-1"})
    assert ok.status_code == 200
    info = _finish_flow(thread, urls, results)

    # the exchange went to the RELAY exactly once, Bearer-authenticated
    assert len(requests_seen) == 1
    request = requests_seen[0]
    assert str(request.url) == RELAY_EXCHANGE_URL
    assert request.headers["Authorization"] == "Bearer platform-token-1"
    assert json_lib.loads(request.read()) == {
        "code": "code-abc",
        "redirect_uri": "http://localhost:8765/callback",
    }
    # no client-side OAuth secret participates in the exchange path
    raw = (
        str(request.url)
        + json_lib.dumps(dict(request.headers))
        + request.read().decode("utf-8")
    )
    assert "legacy-secret-honeypot" not in raw
    assert not request.headers["Authorization"].startswith("Basic ")
    assert info["access_token"] == "secret_relay"


def test_login_via_relay_works_without_any_client_secret(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    require_free(8765)
    # the secret attribute is absent entirely: the relay path cannot depend on it
    settings = make_settings(notion_oauth_client_secret=None)
    thread, urls, results = _run_login_thread(
        settings,
        port=8765,
        timeout=5,
        state="st-nosecret",
        transport=httpx.MockTransport(lambda request: relay_token_response()),
    )
    port = _callback_port(urls[0])
    assert _deliver_callback(port, {"code": "c", "state": "st-nosecret"}).status_code == 200
    assert _finish_flow(thread, urls, results)["access_token"] == "secret_relay"


def test_login_via_relay_requires_platform_token(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    with pytest.raises(NotionError, match="激活"):
        notion_login.login_via_relay(
            make_settings(platform_token=""),
            port=8765,
            open_browser=False,
            timeout=1,
            transport=httpx.MockTransport(lambda request: relay_token_response()),
        )
    assert not (tmp_path / ".env").exists()


def test_login_via_relay_requires_public_client_id(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    with pytest.raises(NotionError, match="NOTION_OAUTH_CLIENT_ID"):
        notion_login.login_via_relay(
            make_settings(notion_oauth_client_id=""),
            port=8765,
            open_browser=False,
            timeout=1,
            transport=httpx.MockTransport(lambda request: relay_token_response()),
        )
    assert not (tmp_path / ".env").exists()


def test_relay_exchange_url_derives_from_transcribe_base():
    settings = make_settings()
    assert notion_login.relay_exchange_url(settings) == RELAY_EXCHANGE_URL
    # unset base → the default deployed relay, never a Notion URL
    assert notion_login.relay_exchange_url(
        make_settings(cloud_transcribe_url="")
    ).startswith("https://")


# -- VAL-SECU-021: token only in the local gitignored .env ----------------------


def test_token_written_only_to_gitignored_env(monkeypatch, tmp_path):
    pkg_root = isolated_env(monkeypatch, tmp_path)
    require_free(8765)
    thread, urls, results = _run_login_thread(
        make_settings(),
        port=8765,
        timeout=5,
        state="st-env",
        transport=httpx.MockTransport(lambda request: relay_token_response()),
    )
    port = _callback_port(urls[0])
    assert _deliver_callback(port, {"code": "c", "state": "st-env"}).status_code == 200
    info = _finish_flow(thread, urls, results)

    env_text = (tmp_path / ".env").read_text("utf-8")
    assert "NOTION_TOKEN=secret_relay" in env_text
    assert info["env_path"] == tmp_path / ".env"
    # the token appears in NO other file under either env search root
    for root in (tmp_path, pkg_root):
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name != ".env":
                text = path.read_text("utf-8", errors="ignore")
                assert "secret_relay" not in text, path
    # and .env itself is gitignored in the repository
    gitignore = (REPO_ROOT / ".gitignore").read_text("utf-8")
    assert any(line.strip() == ".env" for line in gitignore.splitlines())


# -- VAL-SECU-022: callback 8765 → 8766 fallback, loopback, pinned busy error ----


def test_callback_falls_back_to_8766_when_8765_occupied(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    require_free(8766)
    dummy = occupy_port(8765)  # validator-owned occupant
    try:
        thread, urls, results = _run_login_thread(
            make_settings(),
            port=8765,
            timeout=5,
            state="st-fb",
            transport=httpx.MockTransport(lambda request: relay_token_response()),
        )
        # the authorize URL / redirect_uri follow the BOUND port
        assert _callback_port(urls[0]) == 8766
        redirect = parse_qs(urlparse(urls[0]).query)["redirect_uri"][0]
        assert redirect == "http://localhost:8766/callback"
        ok = _deliver_callback(8766, {"code": "c-fb", "state": "st-fb"})
        assert ok.status_code == 200
        info = _finish_flow(thread, urls, results)
        assert info["callback_port"] == 8766
        assert "NOTION_TOKEN=secret_relay" in (tmp_path / ".env").read_text("utf-8")
        # the occupant survives; nothing is ever killed or contacted
        assert_occupant_alive(dummy, 8765)
    finally:
        dummy.close()


def test_callback_server_binds_loopback_only():
    require_free(8766)
    server = notion_login._CallbackServer(8766, "st-loopback")
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] == 8766
    finally:
        server.server_close()


def test_both_callback_ports_occupied_raises_pinned_error(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    first = occupy_port(8765)
    second = occupy_port(8766)
    urls: list[str] = []
    try:
        with pytest.raises(NotionError) as excinfo:
            notion_login.login_via_relay(
                make_settings(),
                port=8765,
                open_browser=False,
                timeout=5,
                transport=httpx.MockTransport(lambda request: relay_token_response()),
                on_url=urls.append,
            )
        # the exact pinned constant, named in code and imported here by name
        assert str(excinfo.value) == CALLBACK_PORTS_BUSY_MESSAGE
        assert "8765" in CALLBACK_PORTS_BUSY_MESSAGE
        assert "8766" in CALLBACK_PORTS_BUSY_MESSAGE
        assert "关闭占用端口的应用后重试" in CALLBACK_PORTS_BUSY_MESSAGE
        # no authorize URL was produced and neither occupant was touched
        assert not urls
        assert_occupant_alive(first, 8765)
        assert_occupant_alive(second, 8766)
    finally:
        first.close()
        second.close()
    assert not (tmp_path / ".env").exists()


def test_callback_ports_are_exactly_the_approved_pair():
    assert CALLBACK_PORTS == (8765, 8766)


def test_login_via_relay_refuses_ports_outside_the_approved_pair(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    for bad_port in (0, 8764, 8767, 9999):
        with pytest.raises(NotionError) as excinfo:
            notion_login.login_via_relay(
                make_settings(),
                port=bad_port,
                open_browser=False,
                timeout=1,
                transport=httpx.MockTransport(lambda request: relay_token_response()),
            )
        assert "8765" in str(excinfo.value)
        assert "8766" in str(excinfo.value)
    assert not (tmp_path / ".env").exists()


# -- VAL-SECU-023: client OAuth errors are genericized --------------------------


@pytest.mark.parametrize("status", [502, 503])
def test_relay_error_response_genericizes_code_and_provider_body(
    status, monkeypatch, tmp_path
):
    isolated_env(monkeypatch, tmp_path)
    require_free(8765)
    seeded_code = "seeded-auth-code-123"
    seeded_provider_body = "upstream-provider-body-xyz"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"detail": f"provider said {seeded_provider_body}"}
        )

    thread, urls, results = _run_login_thread(
        make_settings(),
        port=8765,
        timeout=5,
        state=f"st-{status}",
        transport=httpx.MockTransport(handler),
    )
    port = _callback_port(urls[0])
    ok = _deliver_callback(port, {"code": seeded_code, "state": f"st-{status}"})
    assert ok.status_code == 200
    thread.join(8)
    assert not thread.is_alive()
    assert isinstance(results[0], NotionError)
    message = str(results[0])
    # neither the authorization code nor any provider body is echoed
    assert seeded_code not in message
    assert seeded_provider_body not in message
    assert "provider said" not in message
    # nothing was written on failure
    assert not (tmp_path / ".env").exists()


def test_relay_401_error_is_generic():
    with pytest.raises(NotionError) as excinfo:
        notion_login.relay_exchange_code(
            "platform-token-1",
            RELAY_EXCHANGE_URL,
            "code-xyz",
            "http://localhost:8765/callback",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    401, json={"detail": "missing or invalid session"}
                )
            ),
        )
    message = str(excinfo.value)
    assert "code-xyz" not in message
    assert "missing or invalid session" not in message


def test_relay_connection_failure_is_generic():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to 10.0.0.9:8800")

    with pytest.raises(NotionError) as excinfo:
        notion_login.relay_exchange_code(
            "platform-token-1",
            RELAY_EXCHANGE_URL,
            "code-xyz",
            "http://localhost:8765/callback",
            transport=httpx.MockTransport(handler),
        )
    message = str(excinfo.value)
    assert "code-xyz" not in message
    assert "10.0.0.9" not in message


def test_relay_200_without_token_is_generic():
    with pytest.raises(NotionError) as excinfo:
        notion_login.relay_exchange_code(
            "platform-token-1",
            RELAY_EXCHANGE_URL,
            "code-xyz",
            "http://localhost:8765/callback",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        )
    assert "code-xyz" not in str(excinfo.value)


# -- VAL-SECU-026: ported login states through the relay path --------------------


def test_login_via_relay_full_flow_with_state_rejection(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    require_free(8765)
    thread, urls, results = _run_login_thread(
        make_settings(),
        port=8765,
        timeout=5,
        state="st-relay",
        transport=httpx.MockTransport(lambda request: relay_token_response()),
    )
    assert "state=st-relay" in urls[0]
    port = _callback_port(urls[0])

    wrong = _deliver_callback(port, {"code": "x", "state": "nope"})
    assert wrong.status_code == 400  # stale/foreign state is rejected…
    ok = _deliver_callback(port, {"code": "goodcode", "state": "st-relay"})
    assert ok.status_code == 200  # …without stopping the wait

    info = _finish_flow(thread, urls, results)
    assert info["access_token"] == "secret_relay"
    assert info["workspace_name"] == "测试空间"
    assert info["env_path"] == tmp_path / ".env"
    assert info["authorize_url"] == urls[0]
    assert "NOTION_TOKEN=secret_relay" in (tmp_path / ".env").read_text("utf-8")


def test_login_via_relay_denied_callback(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    require_free(8765)
    exchanges: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        exchanges.append(request)
        return relay_token_response()

    thread, urls, results = _run_login_thread(
        make_settings(),
        port=8765,
        timeout=5,
        state="st-deny",
        transport=httpx.MockTransport(handler),
    )
    port = _callback_port(urls[0])
    denied = _deliver_callback(port, {"error": "access_denied", "state": "st-deny"})
    assert denied.status_code == 400
    thread.join(8)
    assert not thread.is_alive()
    assert isinstance(results[0], NotionError)
    assert "授权被拒绝" in str(results[0])  # the visible denial state
    assert not (tmp_path / ".env").exists()  # nothing written on denial
    assert not exchanges  # denial never reaches the exchange


def test_login_via_relay_timeout_gives_retry_instructions(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    require_free(8765)
    with pytest.raises(NotionError) as excinfo:
        notion_login.login_via_relay(
            make_settings(),
            port=8765,
            open_browser=False,
            timeout=0.3,
            state="st-to",
            transport=httpx.MockTransport(lambda request: relay_token_response()),
        )
    message = str(excinfo.value)
    assert "超时" in message
    assert "重跑一次" in message  # explicit retry instructions
    assert not (tmp_path / ".env").exists()


def test_exchange_is_one_shot_no_retry_on_failure(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    require_free(8765)
    exchange_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        exchange_calls.append(request)
        return httpx.Response(502, json={"detail": "Notion authorization failed"})

    thread, urls, results = _run_login_thread(
        make_settings(),
        port=8765,
        timeout=5,
        state="st-oneshot",
        transport=httpx.MockTransport(handler),
    )
    port = _callback_port(urls[0])
    assert _deliver_callback(port, {"code": "c-one", "state": "st-oneshot"}).status_code == 200
    thread.join(8)
    assert not thread.is_alive()
    assert isinstance(results[0], NotionError)
    # exactly one exchange attempt: a failed exchange is never retried
    assert len(exchange_calls) == 1
    assert not (tmp_path / ".env").exists()


def test_retry_after_failed_exchange_is_a_fresh_login(monkeypatch, tmp_path):
    isolated_env(monkeypatch, tmp_path)
    require_free(8765)
    require_free(8766)
    attempts = {"count": 0}

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(502, json={"detail": "Notion authorization failed"})
        return relay_token_response()

    # first login: the relay exchange fails; the single-use code is consumed
    thread, urls, results = _run_login_thread(
        make_settings(),
        port=8765,
        timeout=5,
        transport=httpx.MockTransport(flaky_handler),
    )
    first_port = _callback_port(urls[0])
    first_state = _state_of(urls[0])
    assert (
        _deliver_callback(first_port, {"code": "c-first", "state": first_state}).status_code
        == 200
    )
    thread.join(8)
    assert not thread.is_alive()
    assert isinstance(results[0], NotionError)

    # retry = a FRESH login: new state, new callback wait, new exchange
    thread2, urls2, results2 = _run_login_thread(
        make_settings(),
        port=8765,
        timeout=5,
        transport=httpx.MockTransport(flaky_handler),
    )
    second_state = _state_of(urls2[0])
    assert second_state != first_state
    assert (
        _deliver_callback(_callback_port(urls2[0]), {"code": "c-second", "state": second_state}).status_code
        == 200
    )
    info = _finish_flow(thread2, urls2, results2)
    assert info["access_token"] == "secret_relay"
    assert attempts["count"] == 2  # one exchange per login, never a code replay
    assert "NOTION_TOKEN=secret_relay" in (tmp_path / ".env").read_text("utf-8")


def test_verify_token_reports_name_at_workspace(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def whoami(self):
            return {"name": "测试集成", "bot": {"owner": {"workspace_name": "测试空间"}}}

    monkeypatch.setattr(
        "pku_sync.notion.get_client", lambda settings=None: FakeClient()
    )
    identity = notion_login.verify_token(SimpleNamespace(notion_token="stored"))
    assert identity == "测试集成 @ 测试空间"


# -- CLI wiring: `pku-sync notion login` uses the relay flow ---------------------


def test_cli_notion_login_uses_relay_flow(monkeypatch, tmp_path):
    import pku_sync.cli as cli

    captured: dict = {}
    verified: list[str] = []

    def fake_login_via_relay(settings, **kwargs):
        captured["settings"] = settings
        captured["kwargs"] = kwargs
        return {
            "access_token": "unused",
            "workspace_name": "测试空间",
            "env_path": tmp_path / ".env",
            "authorize_url": "https://api.notion.com/v1/oauth/authorize?...",
            "callback_port": 8765,
        }

    def legacy_boom(*args, **kwargs):
        raise AssertionError("the CLI must not use the legacy local-secret login")

    monkeypatch.setattr(notion_login, "login_via_relay", fake_login_via_relay)
    monkeypatch.setattr(notion_login, "login", legacy_boom)
    monkeypatch.setattr(
        notion_login,
        "verify_token",
        lambda settings=None: verified.append("called") or "测试集成 @ 测试空间",
    )

    cli.notion_login()  # defaults: port 8765, browser on, 300s timeout

    assert captured["kwargs"]["port"] == 8765
    assert captured["kwargs"]["open_browser"] is True
    assert captured["kwargs"]["timeout"] == 300.0
    assert callable(captured["kwargs"]["on_url"])
    assert verified == ["called"]  # post-exchange verification runs
