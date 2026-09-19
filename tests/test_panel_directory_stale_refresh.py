"""Regression coverage for invalidating the directory after refresh errors."""

from __future__ import annotations

from fastapi.testclient import TestClient

from pku_sync.panel.directory import DirectoryService
from pku_sync.panel.fake_directory import PanelDirectoryProvider
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L1, build_panel_workspace
from pku_sync.panel.webapi import create_app


class FlakyProvider:
    def __init__(self):
        self._inner = PanelDirectoryProvider(build_panel_workspace())
        self._loads = 0

    def load(self):
        self._loads += 1
        if self._loads == 2:
            raise RuntimeError("refresh failed")
        return self._inner.load()

    def resolve_launch(self, data, target_id, *, course_id=None):
        return self._inner.resolve_launch(data, target_id, course_id=course_id)


def test_failed_refresh_invalidates_material_snapshot_until_successful_retry():
    service = DirectoryService(FlakyProvider())
    client = TestClient(create_app(directory_service=service))

    assert client.get("/api/directory").status_code == 200
    assert client.get(
        f"/api/courses/{COURSE_NET}/materials",
        params={"view": "lecture", "lecture_id": NET_L1},
    ).status_code == 200

    failed_refresh = client.get("/api/directory")
    assert failed_refresh.status_code == 503
    assert client.get("/api/sync/state").json()["state"] == "error"

    stale_read = client.get(
        f"/api/courses/{COURSE_NET}/materials",
        params={"view": "lecture", "lecture_id": NET_L1},
    )
    assert stale_read.status_code == 503

    recovered = client.get("/api/directory")
    assert recovered.status_code == 200
    restored_read = client.get(
        f"/api/courses/{COURSE_NET}/materials",
        params={"view": "lecture", "lecture_id": NET_L1},
    )
    assert restored_read.status_code == 200
