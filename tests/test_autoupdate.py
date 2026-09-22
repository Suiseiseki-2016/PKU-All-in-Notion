"""Autoupdate contract: GitHub manifest, notify-only flow, safe next-start apply."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from pku_sync import autoupdate
from pku_sync.panel import webapi


def make_service(tmp_path: Path, *, current: str = "0.1.0", fetcher=None, runner=None):
    options = {
        "current_version": current,
        "state_path": tmp_path / "data" / "update-pending.json",
    }
    if fetcher is not None:
        options["fetcher"] = fetcher
    if runner is not None:
        options["upgrade_runner"] = runner
    return autoupdate.UpdateService(**options)


def test_github_release_manifest_has_hard_timeout_and_reports_new_version(monkeypatch, tmp_path):
    observed = {}

    def fake_get(url, *, headers, timeout):
        observed.update(url=url, headers=headers, timeout=timeout)
        return httpx.Response(
            200,
            json={"tag_name": "v0.2.0"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(autoupdate.httpx, "get", fake_get)
    service = make_service(tmp_path)

    result = service.check()

    assert observed["url"] == autoupdate.GITHUB_RELEASE_URL
    assert observed["timeout"] == autoupdate.UPDATE_TIMEOUT_SECONDS
    assert result == autoupdate.UpdateCheck(
        current_version="0.1.0", available_version="0.2.0", status="available"
    )


def test_manifest_check_reports_up_to_date_and_never_queues_an_upgrade(tmp_path):
    service = make_service(tmp_path, fetcher=lambda: "0.1.0")

    assert service.check().status == "up_to_date"
    assert not (tmp_path / "data" / "update-pending.json").exists()


def test_unreachable_manifest_skips_silently(tmp_path):
    def unreachable():
        raise httpx.ConnectError("offline")

    result = make_service(tmp_path, fetcher=unreachable).check()

    assert result == autoupdate.UpdateCheck(
        current_version="0.1.0", available_version=None, status="unavailable"
    )


def test_only_explicit_apply_request_queues_the_next_start_upgrade(tmp_path):
    service = make_service(tmp_path, fetcher=lambda: "0.2.0")
    assert service.check().status == "available"
    assert not service.state_path.exists()

    service.request_apply("0.2.0")

    assert json.loads(service.state_path.read_text("utf-8")) == {
        "status": "requested",
        "version": "0.2.0",
    }


def test_failed_or_interrupted_upgrade_defers_and_leaves_env_and_data_intact(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    env_file = app_dir / ".env"
    env_file.write_text("PLATFORM_TOKEN=kept-local\n", "utf-8")
    data_file = app_dir / "data" / "index.json"
    data_file.parent.mkdir()
    data_file.write_text('{"course":"kept"}', "utf-8")

    def interrupted(command, **kwargs):
        assert command == ["uv", "tool", "upgrade", "pku-course-sync"]
        raise KeyboardInterrupt()

    service = autoupdate.UpdateService(
        current_version="0.1.0",
        state_path=app_dir / "data" / "update-pending.json",
        fetcher=lambda: "0.2.0",
        upgrade_runner=interrupted,
    )
    service.check()
    service.request_apply("0.2.0")

    outcome = service.apply_pending()

    assert outcome == autoupdate.ApplyOutcome(status="deferred", version="0.2.0")
    assert env_file.read_text("utf-8") == "PLATFORM_TOKEN=kept-local\n"
    assert data_file.read_text("utf-8") == '{"course":"kept"}'
    assert service.panel_state() == {
        "version": "0.1.0",
        "status": "deferred",
        "available_version": "0.2.0",
    }


def test_successful_next_start_apply_clears_request_for_new_version(tmp_path):
    calls = []

    def upgraded(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    service = make_service(tmp_path, fetcher=lambda: "0.2.0", runner=upgraded)
    service.check()
    service.request_apply("0.2.0")

    assert service.apply_pending() == autoupdate.ApplyOutcome(status="applied", version="0.2.0")
    assert calls == [
        (
            ["uv", "tool", "upgrade", "pku-course-sync"],
            {"check": True, "timeout": autoupdate.UPGRADE_TIMEOUT_SECONDS},
        )
    ]
    assert not service.state_path.exists()


def test_panel_surfaces_version_notice_action_and_silent_unreachable_state(tmp_path):
    available = make_service(tmp_path, fetcher=lambda: "0.2.0")
    settings = SimpleNamespace(
        data_dir=tmp_path,
        cloud_transcribe_url="https://relay.invalid/v1/transcribe",
        platform_token="",
        transcription_backend="local",
    )
    client = TestClient(webapi.create_app(settings=settings, update_service=available))

    shell = client.get("/app")
    assert shell.status_code == 200
    assert "Version {version}" in shell.text
    assert "new version {version} available" in shell.text
    assert "Apply on next start" in shell.text
    assert "Retry on next start" in shell.text
    assert '"data-version"' in client.get("/app/app.js").text

    payload = client.get("/api/update").json()
    assert payload == {
        "version": "0.1.0",
        "status": "available",
        "available_version": "0.2.0",
    }
    queued = client.post("/api/update/request", json={"version": "0.2.0"})
    assert queued.status_code == 200
    assert queued.json()["status"] == "requested"

    silent = make_service(tmp_path / "offline", fetcher=lambda: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    offline_client = TestClient(webapi.create_app(settings=settings, update_service=silent))
    assert offline_client.get("/api/update").json() == {"version": "0.1.0", "status": "unavailable"}
    assert offline_client.get("/healthz").json() == {"ok": True}


def test_panel_state_reuses_manifest_check_for_process_lifetime(tmp_path):
    calls = []

    def fetch():
        calls.append("check")
        return "0.2.0"

    service = make_service(tmp_path, fetcher=fetch)
    assert service.panel_state()["status"] == "available"
    assert service.panel_state()["status"] == "available"
    assert calls == ["check"]


def test_corrupt_requested_version_is_cleared_instead_of_staying_queued(tmp_path):
    service = make_service(tmp_path)
    service.state_path.parent.mkdir(parents=True)
    service.state_path.write_text(
        '{"status":"requested","version":"not-a-version"}', encoding="utf-8"
    )

    assert service.apply_pending() == autoupdate.ApplyOutcome(status="none", version=None)
    assert not service.state_path.exists()
