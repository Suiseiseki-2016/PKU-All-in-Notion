"""Panel tests: routing, single-flight, status parsing (TestClient, no network)."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pku_sync.panel import jobs as jobs_mod
from pku_sync.panel import webapi


class FakeRunner:
    """A runner double: records submits, reports a fixed state."""

    def __init__(self, running: bool = False):
        self.submitted: list[str] = []
        self.running = running

    def submit(self, kind: str):
        self.submitted.append(kind)
        if self.running:
            return None  # busy
        return jobs_mod.Job(kind=kind, state="running")

    def current(self):
        return None

    def last(self):
        return None


def make_settings(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=tmp_path,
        cloud_transcribe_url="https://pku.aeoluswu.info/v1/transcribe",
        platform_token="",
        transcription_backend="local",
    )


def write_index(tmp_path) -> None:
    """One course with one indexed recording, the minimal real data tree."""
    rec_dir = tmp_path / "认知心理学" / "recordings"
    rec_dir.mkdir(parents=True)
    index = [
        {"course_id": "_c1_", "title": "第一讲 绪论", "recorded_at": "2026-09-09 08:00:00"}
    ]
    (rec_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False), "utf-8")


def client(tmp_path, runner=None) -> TestClient:
    return TestClient(webapi.create_app(make_settings(tmp_path)))


def test_index_redirects_to_current_student_workspace(tmp_path):
    response = client(tmp_path).get("/", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/app"
    assert client(tmp_path).get("/app").status_code == 200
    for retired in ("/api/status", "/partials/status", "/recordings"):
        assert client(tmp_path).get(retired).status_code == 404
    assert client(tmp_path).post("/actions/daily").status_code == 404
    assert client(tmp_path).post("/actions/automate").status_code == 404


def test_unknown_action_is_404(tmp_path):
    response = client(tmp_path).post("/actions/nap")
    assert response.status_code == 404


def test_healthz(tmp_path):
    response = client(tmp_path).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
