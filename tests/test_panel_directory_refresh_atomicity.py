"""Additive coverage for refresh/read snapshot atomicity."""

from __future__ import annotations

import threading

import pytest

import pku_sync.panel.directory as directory_module
from pku_sync.panel.directory import DirectoryApiError, DirectoryService
from pku_sync.panel.fake_directory import PanelDirectoryProvider
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L1, build_panel_workspace


class FailOnRefreshProvider:
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


def test_material_read_cannot_finish_from_snapshot_after_refresh_enters_error(monkeypatch):
    service = DirectoryService(FailOnRefreshProvider())
    service.load()

    read_started = threading.Event()
    release_read = threading.Event()
    result = {}
    original_build = directory_module.build_lecture_view

    def paused_build(data, course, lecture):
        read_started.set()
        assert release_read.wait(timeout=5)
        return original_build(data, course, lecture)

    monkeypatch.setattr(directory_module, "build_lecture_view", paused_build)

    def read_materials():
        try:
            result["value"] = service.material_view(
                COURSE_NET,
                "lecture",
                lecture_id=NET_L1,
            )
        except Exception as exc:  # noqa: BLE001 - assert the service error below
            result["error"] = exc

    reader = threading.Thread(target=read_materials)
    reader.start()
    assert read_started.wait(timeout=5)

    with pytest.raises(DirectoryApiError) as refresh_error:
        service.load()
    assert refresh_error.value.status == 503
    assert service.sync_snapshot()["state"] == "error"

    release_read.set()
    reader.join(timeout=5)
    assert isinstance(result.get("error"), DirectoryApiError)
    assert "value" not in result


class OverlappingProvider:
    def __init__(self):
        self._inner = PanelDirectoryProvider(build_panel_workspace())
        self.started = {1: threading.Event(), 2: threading.Event()}
        self.release = {1: threading.Event(), 2: threading.Event()}
        self.calls = 0

    def load(self):
        self.calls += 1
        call = self.calls
        self.started[call].set()
        assert self.release[call].wait(timeout=5)
        if call == 1:
            raise RuntimeError("older refresh failed")
        return self._inner.load()

    def resolve_launch(self, data, target_id, *, course_id=None):
        return self._inner.resolve_launch(data, target_id, course_id=course_id)


def test_older_failed_refresh_cannot_erase_newer_successful_snapshot():
    provider = OverlappingProvider()
    service = DirectoryService(provider)
    service._refresh_generation = 0
    results = {}

    def refresh(name):
        try:
            results[name] = service.load()
        except Exception as exc:  # noqa: BLE001 - assert completion ordering
            results[name] = exc

    first = threading.Thread(target=refresh, args=("first",))
    first.start()
    assert provider.started[1].wait(timeout=5)
    second = threading.Thread(target=refresh, args=("second",))
    second.start()
    assert provider.started[2].wait(timeout=5)

    provider.release[2].set()
    second.join(timeout=5)
    assert not second.is_alive()
    assert isinstance(results["second"], dict)

    provider.release[1].set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert isinstance(results["first"], DirectoryApiError)

    assert service.sync_snapshot()["state"] == "done"
    assert service.sync_snapshot()["error_reason"] is None
    assert service.material_view(COURSE_NET, "lecture", lecture_id=NET_L1)["view"] == "lecture"
