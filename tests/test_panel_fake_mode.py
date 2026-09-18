"""Seeded-fake startup mode tests (the reusable M3 fixture).

The panel's ``--fake`` mode must be a single, reusable foundation every later
browser-verified M3 panel feature builds on: seeded fixtures covering mapped /
unmapped / inferred-empty courses with honeypot strings, plus slow and
fault-injected variants. These tests pin that contract so later features do
not re-derive their own fakes.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from pku_sync.panel.directory import (
    STATE_DONE,
    STATE_ERROR,
    DirectoryService,
)
from pku_sync.panel.fake_directory import (
    FAULT_LAUNCH_TARGET,
    PANEL_FAKE_SEMESTER,
    SLOW_DELAY,
    FaultLaunchProvider,
    PanelDirectoryProvider,
    build_fake_directory,
)
from pku_sync.panel.fake_workspace import (
    COURSE_COG,
    COURSE_DEV,
    COURSE_NET,
    COG_L1,
    DEV_L1,
    NET_L1,
    NET_L2,
    PANEL_FORBIDDEN_STRINGS,
    build_panel_workspace,
)
from pku_sync.panel.webapi import create_app


def make_client(**kwargs):
    service = build_fake_directory(**kwargs)
    return TestClient(create_app(directory_service=service)), service


def test_seeded_workspace_covers_mapped_unmapped_and_inferred_empty():
    """The three course profiles every material-view state needs."""
    ws = build_panel_workspace()
    client, service = make_client()
    body = client.get("/api/directory").json()
    courses = {c["title"]: c for c in body["courses"]}
    assert set(courses) == {"计算机网络", "发展心理学", "认知心理学", "计算概论B"}
    assert courses["计算机网络"]["mapping_state"] == "mapped"
    assert courses["发展心理学"]["mapping_state"] == "unmapped"
    # 计算机网络第一讲 mapped with a confirmed + inferred subsection;
    # 第二讲 inferred-only; 认知心理学 first lecture has zero materials.
    net = client.get(
        f"/api/courses/{COURSE_NET}/materials", params={"view": "lecture", "lecture_id": NET_L1}
    ).json()
    assert net["mapped"] is True
    assert net["confirmed"] and net["inferred"]
    net2 = client.get(
        f"/api/courses/{COURSE_NET}/materials", params={"view": "lecture", "lecture_id": NET_L2}
    ).json()
    assert net2["mapped"] is False and net2["inferred"]
    dev = client.get(
        f"/api/courses/{COURSE_DEV}/materials", params={"view": "lecture", "lecture_id": DEV_L1}
    ).json()
    assert dev["mapped"] is False and dev["inferred"]
    cog = client.get(
        f"/api/courses/{COURSE_COG}/materials", params={"view": "lecture", "lecture_id": COG_L1}
    ).json()
    assert cog["mapped"] is False and cog["confirmed"] == [] and cog["inferred"] == []


def test_seeded_honeypots_are_forbidden_from_every_panel_payload():
    """Every branch the browser features will render stays honeypot-clean."""
    import json

    client, service = make_client()
    outputs = []
    payload = client.get("/api/directory").json()
    outputs.append(payload)
    for params in ({"view": "all"}, {"view": "type"}, {"view": "lecture", "lecture_id": NET_L1}):
        outputs.append(
            client.get(f"/api/courses/{COURSE_NET}/materials", params=params).json()
        )
    outputs.append(client.post("/api/launch", json={"target_id": NET_L1}).json())
    dumped = json.dumps(outputs, ensure_ascii=False)
    for needle in PANEL_FORBIDDEN_STRINGS:
        assert needle not in dumped


def test_fake_mode_runs_through_the_real_adapter():
    """The fake is not a hand-rolled payload: the REAL M2 adapter reads it."""
    ws = build_panel_workspace()
    _, service = make_client(workspace=ws)
    data = service.ensure_loaded()
    # the adapter's own confirmed/inferred queries produced the seeded states
    assert len(data.confirmed_materials_for_lecture(NET_L1)) >= 1
    assert len(data.inferred_materials_for_lecture(NET_L1)) >= 1
    assert len(data.confirmed_materials_for_lecture(DEV_L1)) == 0
    assert len(data.inferred_materials_for_lecture(DEV_L1)) == 1


def test_fake_service_requires_no_notion_token_or_network():
    """--fake must work with an unconfigured local environment."""
    from types import SimpleNamespace

    class NoopRunner:
        def submit(self, kind):
            return None

        def current(self):
            return None

        def last(self):
            return None

    settings = SimpleNamespace(
        data_dir="data", platform_token="", notion_token="", transcription_backend="local"
    )
    service = build_fake_directory()
    app = create_app(settings=settings, runner=NoopRunner(), directory_service=service)
    client = TestClient(app)
    assert client.get("/api/directory").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}


def test_normal_variant_loads_to_done():
    client, service = make_client()
    assert client.get("/api/directory").status_code == 200
    assert client.get("/api/sync/state").json()["state"] == STATE_DONE


def test_slow_variant_completes_and_takes_noticeably_longer():
    client, service = make_client(variant="slow", slow_delay=SLOW_DELAY)
    assert client.get("/api/directory").status_code == 200
    assert client.get("/api/sync/state").json()["state"] == STATE_DONE


def test_fault_variant_lands_in_the_error_sync_state_every_time():
    client, service = make_client(variant="fault")
    for _ in range(2):  # deterministic, retry cannot fake success
        response = client.get("/api/directory")
        assert response.status_code == 503
    sync = client.get("/api/sync/state").json()
    assert sync["state"] == STATE_ERROR
    assert "学习中心" in sync["error_reason"]

    # material views and launches never silently fabricate a directory
    assert client.get(f"/api/courses/{COURSE_NET}/materials", params={"view": "all"}).status_code == 503
    launch = client.post("/api/launch", json={"target_id": NET_L1})
    assert launch.status_code == 404  # missing-mapping: no loaded directory to open from


def test_fault_launch_variant_fails_only_the_flagged_lecture():
    client, service = make_client(variant="fault-launch")
    client.get("/api/directory")  # directory loads normally
    failed = client.post("/api/launch", json={"target_id": FAULT_LAUNCH_TARGET})
    assert failed.status_code == 502
    assert "打开没有成功" in failed.json()["detail"]
    # normal launches still open the stored identity
    ok = client.post("/api/launch", json={"target_id": NET_L2})
    assert ok.status_code == 200
    assert ok.json()["status"] == "opened"


def test_unknown_variant_is_rejected_explicitly():
    with pytest.raises(ValueError) as excinfo:
        build_fake_directory(variant="wat")
    assert "normal" in str(excinfo.value) and "fault-launch" in str(excinfo.value)


def test_fake_launch_paths_never_search_or_touch_any_client():
    client, service = make_client()
    client.get("/api/directory")
    before = list(service.provider.client.calls)
    for target in (NET_L1, NET_L2, "bogus-target", ""):
        client.post("/api/launch", json={"target_id": target, "course_id": COURSE_NET})
    after = list(service.provider.client.calls)
    assert after == before  # launches add zero client calls


def test_fake_providers_are_swappable_through_the_service():
    ws = build_panel_workspace()
    provider = PanelDirectoryProvider(ws, semester=PANEL_FAKE_SEMESTER)
    service = DirectoryService(provider)
    data = service.ensure_loaded()
    assert data.semester == PANEL_FAKE_SEMESTER
    assert data.hub.title == "Class Notes 2026 下半学期"
    assert isinstance(provider, PanelDirectoryProvider)
    fault = FaultLaunchProvider(ws, semester=PANEL_FAKE_SEMESTER)
    result = fault.resolve_launch(data, FAULT_LAUNCH_TARGET)
    assert result.status == "failed"
    result = fault.resolve_launch(data, NET_L2)
    assert result.status == "opened"
