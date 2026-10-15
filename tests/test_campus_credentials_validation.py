"""Credential validation must finish before replacing the saved campus identity."""
from types import SimpleNamespace
import threading

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from pku_sync import auth
from pku_sync.panel import recordings_api


def endpoint(root, monkeypatch):
    settings = SimpleNamespace(data_dir=root / "data", platform_token="account",
                               pku_username="original", pku_password="original-secret")
    manager = recordings_api.RecordingWorkManager(settings)
    app = FastAPI()
    recordings_api.add_recording_routes(app, manager)
    path = root / ".env"
    path.write_text("PKU_USERNAME=original\nPKU_PASSWORD=original-secret\n", "utf-8")
    monkeypatch.setattr(recordings_api, "env_file_path", lambda _: path)
    route = next(r.endpoint for r in app.routes if r.path == "/api/recordings/credentials")
    return route, settings, path


@pytest.mark.parametrize("failure,status", [(auth.CampusLoginRejected("检查账号和密码"), 401),
    (httpx.ReadTimeout("private-secret URL"), 503), (RuntimeError("private-secret upstream"), 503)])
def test_rejected_or_unreachable_login_never_replaces_credentials(tmp_path, monkeypatch, failure, status):
    route, settings, path = endpoint(tmp_path, monkeypatch)
    before = path.read_bytes()
    def reject(**kwargs):
        raise failure
    monkeypatch.setattr(auth, "get_session", reject)
    with pytest.raises(HTTPException) as caught:
        route(recordings_api.CampusCredentials(username="replacement", password="wrong"))
    assert caught.value.status_code == status
    assert "private-secret" not in caught.value.detail
    assert path.read_bytes() == before
    assert (settings.pku_username, settings.pku_password) == ("original", "original-secret")


def test_success_closes_verified_session_then_saves(tmp_path, monkeypatch):
    route, settings, path = endpoint(tmp_path, monkeypatch)
    calls = []
    def login(**kwargs):
        calls.append(kwargs)
        assert "replacement" not in path.read_text("utf-8")
        return SimpleNamespace(close=lambda: calls.append("closed"))
    monkeypatch.setattr(auth, "get_session", login)
    assert route(recordings_api.CampusCredentials(username=" replacement ", password="correct")) == {"configured": True, "verified": True}
    assert calls == [{"username": "replacement", "password": "correct"}, "closed"]
    assert settings.pku_username == "replacement"
    assert "PKU_PASSWORD=correct" in path.read_text("utf-8")


def test_account_change_during_validation_cannot_save_to_new_account(tmp_path, monkeypatch):
    route, settings, path = endpoint(tmp_path, monkeypatch)
    before = path.read_bytes()
    def login(**kwargs):
        settings.platform_token = "other-account"
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(auth, "get_session", login)
    with pytest.raises(HTTPException) as caught:
        route(recordings_api.CampusCredentials(username="replacement", password="correct"))
    assert caught.value.status_code == 409
    assert path.read_bytes() == before


def test_duplicate_validation_is_rejected_while_first_request_is_pending(tmp_path, monkeypatch):
    route, settings, path = endpoint(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    result = []
    def login(**kwargs):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(auth, "get_session", login)
    body = recordings_api.CampusCredentials(username="replacement", password="correct")
    thread = threading.Thread(target=lambda: result.append(route(body)))
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(HTTPException) as caught:
            route(body)
        assert caught.value.status_code == 409
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and result == [{"configured": True, "verified": True}]


def test_iaaa_rejection_is_typed_and_hides_upstream_response(monkeypatch):
    def respond(request):
        body = {"success": True, "key": "fixture"} if request.method == "GET" else {
            "success": False, "errors": {"msg": "private-secret"}}
        return httpx.Response(200, json=body)
    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(auth, "new_client", lambda: client)
    monkeypatch.setattr(auth, "_encrypt_password", lambda *_: "encrypted")
    with pytest.raises(auth.CampusLoginRejected) as caught:
        auth._login("student", "wrong")
    assert "private-secret" not in str(caught.value)
    assert client.is_closed
