"""Panel Notion-connection surface (VAL-META-020).

The student panel shows whether the Notion workspace is connected, revokes
the connection (which CLEARS the local token), and reconnects through the
relay OAuth flow. These tests pin the API contract behind the browser
assertions:

- connected / disconnected state payloads with the approved copy;
- the Notion token never appears in any panel payload (seeded-token scan);
- revoking clears the token from the local .env AND from the live settings;
- revoking drops the cached directory so a disconnected panel can never
  render the previously loaded index (no stale data);
- reconnect restores the connected state; a failed reconnect stays generic;
- concurrent reconnects get the pinned single-flight busy copy.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pku_sync.notion import NotionError
from pku_sync.panel.connection import (
    CONNECTED_COPY,
    CONNECT_BUSY_MESSAGE,
    CONNECT_FAILED_COPY,
    CONNECTING_COPY,
    DISCONNECT_TOAST,
    DISCONNECTED_COPY,
    DISCONNECTED_DIRECTORY_ACTION,
    DISCONNECTED_DIRECTORY_COPY,
    DISCONNECTED_DIRECTORY_TITLE,
    RECONNECT_TOAST,
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    ConnectionService,
    FakeConnectionProvider,
    RealConnectionProvider,
    build_fake_connection_service,
    make_connection_service,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.webapi import create_app

SEEDED_TOKEN = "secret_seeded_notion_token_c0ffee"


def make_settings(tmp_path, token: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=tmp_path,
        cloud_transcribe_url="https://relay.example/v1/transcribe",
        platform_token="",
        transcription_backend="local",
        notion_token=token,
    )


def make_app(service, settings=None, directory_service=None):
    """A panel app whose only live surface is the connection service."""
    return TestClient(
        create_app(
            settings=settings or SimpleNamespace(data_dir="data", platform_token=""),
            runner=_NoopRunner(),
            directory_service=directory_service or build_fake_directory(),
            connection_service=service,
        )
    )


class _NoopRunner:
    def submit(self, kind):
        return None

    def current(self):
        return None

    def last(self):
        return None


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- state payloads -------------------------------------------------------------


def test_connected_state_uses_the_approved_copy_and_exact_key_set(tmp_path):
    service = make_connection_service(make_settings(tmp_path, token=SEEDED_TOKEN))
    body = make_app(service).get("/api/connection").json()
    assert body == {
        "state": STATE_CONNECTED,
        "connected": True,
        "status_label": CONNECTED_COPY,
        "error": None,
    }


def test_disconnected_state_uses_the_approved_copy(tmp_path):
    service = make_connection_service(make_settings(tmp_path))
    body = make_app(service).get("/api/connection").json()
    assert body["state"] == STATE_DISCONNECTED
    assert body["connected"] is False
    assert body["status_label"] == DISCONNECTED_COPY
    assert body["error"] is None


def test_whitespace_only_token_counts_as_disconnected(tmp_path):
    service = make_connection_service(make_settings(tmp_path, token="   "))
    assert service.snapshot()["connected"] is False


# -- the token never appears in a payload --------------------------------------


def test_connection_payloads_never_contain_the_notion_token(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(f"NOTION_TOKEN={SEEDED_TOKEN}\n", encoding="utf-8")
    settings = make_settings(tmp_path, token=SEEDED_TOKEN)
    service = make_connection_service(settings, env_path=env_path)
    client = make_app(service, settings=settings)
    responses = [
        client.get("/api/connection"),
        client.post("/api/connection/disconnect"),
        client.get("/api/connection"),
    ]
    for response in responses:
        assert response.status_code == 200
        assert SEEDED_TOKEN not in response.text
        assert "secret_" not in json.dumps(response.json(), ensure_ascii=False)


# -- revoke clears the local token ---------------------------------------------


def test_disconnect_clears_the_token_from_the_env_file_and_settings(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"PKU_USERNAME=someone\nNOTION_TOKEN={SEEDED_TOKEN}\nDATA_DIR=E:/data\n",
        encoding="utf-8",
    )
    settings = make_settings(tmp_path, token=SEEDED_TOKEN)
    service = make_connection_service(settings, env_path=env_path)

    body = make_app(service, settings=settings).post("/api/connection/disconnect").json()

    assert body["connected"] is False
    assert body["status_label"] == DISCONNECTED_COPY
    text = env_path.read_text(encoding="utf-8")
    assert SEEDED_TOKEN not in text
    assert "NOTION_TOKEN=\n" in text  # the key is emptied, not deleted
    assert "PKU_USERNAME=someone" in text  # every other line preserved
    assert settings.notion_token == ""


def test_disconnect_is_idempotent(tmp_path):
    settings = make_settings(tmp_path, token=SEEDED_TOKEN)
    service = make_connection_service(settings, env_path=tmp_path / ".env")
    client = make_app(service, settings=settings)
    first = client.post("/api/connection/disconnect").json()
    second = client.post("/api/connection/disconnect").json()
    assert first["connected"] is second["connected"] is False


# -- no stale directory data after a revoke ------------------------------------


def test_disconnect_drops_the_cached_directory_so_nothing_stale_is_served(tmp_path):
    directory = build_fake_directory()
    service = build_fake_connection_service(directory_service=directory)
    client = make_app(service, directory_service=directory)

    client.get("/api/directory")
    reads_after_first_load = len(directory.provider.client.calls)
    client.get("/api/courses/x/materials", params={"view": "all"})  # cached read
    assert len(directory.provider.client.calls) == reads_after_first_load

    client.post("/api/connection/disconnect")
    client.get("/api/directory")  # a fresh read, not the dropped cache
    assert len(directory.provider.client.calls) > reads_after_first_load


# -- reconnect ------------------------------------------------------------------


def test_reconnect_runs_the_relay_login_flow_and_restores_connected_state(tmp_path):
    settings = make_settings(tmp_path)
    calls = []

    def fake_login(login_settings, **kwargs):
        calls.append(kwargs)
        return {"access_token": SEEDED_TOKEN}

    service = make_connection_service(
        settings, env_path=tmp_path / ".env", login=fake_login
    )
    client = make_app(service, settings=settings)

    started = client.post("/api/connection/connect")
    assert started.status_code == 200
    assert started.json()["state"] in (STATE_CONNECTING, STATE_CONNECTED)

    assert wait_for(lambda: service.snapshot()["connected"] is True)
    body = client.get("/api/connection").json()
    assert body["state"] == STATE_CONNECTED
    assert body["status_label"] == CONNECTED_COPY
    assert SEEDED_TOKEN not in json.dumps(body, ensure_ascii=False)
    assert settings.notion_token == SEEDED_TOKEN  # local only
    assert calls and calls[0]["open_browser"] is True


def test_reconnect_failure_is_generic_and_never_echoes_the_provider_detail(tmp_path):
    settings = make_settings(tmp_path)
    leak = "notion said: invalid_grant code=abcdef123456"

    def failing_login(login_settings, **kwargs):
        raise NotionError(leak)

    service = make_connection_service(
        settings, env_path=tmp_path / ".env", login=failing_login
    )
    client = make_app(service, settings=settings)
    client.post("/api/connection/connect")

    assert wait_for(lambda: service.snapshot()["error"] is not None)
    body = client.get("/api/connection").json()
    assert body["state"] == STATE_DISCONNECTED
    assert body["connected"] is False
    assert body["error"] == CONNECT_FAILED_COPY
    rendered = json.dumps(body, ensure_ascii=False)
    assert leak not in rendered and "invalid_grant" not in rendered


def test_concurrent_reconnect_is_rejected_with_the_pinned_busy_copy(tmp_path):
    settings = make_settings(tmp_path)
    release = threading.Event()
    entered = threading.Event()

    def slow_login(login_settings, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return {"access_token": SEEDED_TOKEN}

    service = make_connection_service(
        settings, env_path=tmp_path / ".env", login=slow_login
    )
    client = make_app(service, settings=settings)

    first = client.post("/api/connection/connect")
    assert first.status_code == 200
    assert entered.wait(timeout=5)
    assert service.snapshot()["state"] == STATE_CONNECTING
    assert service.snapshot()["status_label"] == CONNECTING_COPY

    busy = client.post("/api/connection/connect")
    assert busy.status_code == 409
    assert busy.json()["detail"] == CONNECT_BUSY_MESSAGE

    release.set()
    assert wait_for(lambda: service.snapshot()["connected"] is True)
    # accepted again after the flight finishes
    assert client.post("/api/connection/connect").status_code == 200


def test_reconnect_after_disconnect_round_trips_through_the_fake_provider():
    service = build_fake_connection_service()
    client = make_app(service)
    assert client.get("/api/connection").json()["connected"] is True
    assert client.post("/api/connection/disconnect").json()["connected"] is False
    restored = client.post("/api/connection/connect").json()
    assert restored["connected"] is True
    assert restored["state"] == STATE_CONNECTED


def test_fake_provider_never_touches_an_env_file(tmp_path):
    env_path = tmp_path / ".env"
    service = ConnectionService(FakeConnectionProvider())
    service.disconnect()
    service.connect()
    assert not env_path.exists()


def test_real_provider_reads_connection_from_the_live_settings(tmp_path):
    settings = make_settings(tmp_path)
    provider = RealConnectionProvider(settings, env_path=tmp_path / ".env")
    assert provider.connected() is False
    settings.notion_token = SEEDED_TOKEN
    assert provider.connected() is True


# -- pinned copy ----------------------------------------------------------------


def test_connection_copy_matches_the_approved_prototype():
    assert CONNECTED_COPY == "内容将同步至你的空间 · 已连接"
    assert DISCONNECTED_COPY == "已断开 · 可随时重新连接"
    assert DISCONNECT_TOAST == "已断开 Notion 连接，可随时重新连接。"
    assert RECONNECT_TOAST == "已重新连接 Notion 学习空间。"


def test_disconnected_directory_state_copy_is_dedicated_and_distinct():
    from pku_sync.panel.directory import SYNC_ERROR_GENERIC, SYNC_ERROR_READ

    # the disconnected directory state is its own state: not a sync error and
    # not the generic empty copy
    assert DISCONNECTED_DIRECTORY_TITLE == "Notion 已断开"
    assert "重新连接" in DISCONNECTED_DIRECTORY_COPY
    assert DISCONNECTED_DIRECTORY_ACTION == "重新连接 Notion"
    for reason in (SYNC_ERROR_GENERIC, SYNC_ERROR_READ):
        assert DISCONNECTED_DIRECTORY_COPY != reason
    assert "还没有已索引的课程" not in DISCONNECTED_DIRECTORY_COPY
