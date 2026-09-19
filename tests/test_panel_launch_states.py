"""Launch feedback, missing-page entries, and honest sync-state surfaces."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import COURSE_CS, NET_L1
from pku_sync.panel.student_ui import PANEL_COPY, student_page
from pku_sync.panel.webapi import create_app


def make_client(variant="normal"):
    service = build_fake_directory(variant=variant)
    return TestClient(create_app(directory_service=service)), service


def test_fault_launch_is_recoverable_and_has_no_success_url():
    client, service = make_client("fault-launch")
    assert client.get("/api/directory").status_code == 200
    before = list(service.provider.client.calls)
    response = client.post("/api/launch", json={"target_id": NET_L1})
    assert response.status_code == 502
    assert response.json() == {"detail": "打开没有成功，请稍后重试。"}
    assert service.provider.client.calls == before


def test_missing_page_target_is_explicit_and_never_fabricates_url():
    client, _ = make_client()
    assert client.get("/api/directory").status_code == 200
    body = client.post(
        "/api/launch", json={"target_id": "missing-lecture", "course_id": COURSE_CS}
    ).json()
    assert body["status"] == "missing_mapping"
    assert body["url"] is None
    assert body["fallback"]["id"] == COURSE_CS


def test_student_launch_uses_id_only_same_tab_and_retry_copy():
    page = student_page()
    assert "window.open(" not in page
    assert "重新打开" in page
    assert "打开没有成功" in page


def test_student_ui_ships_missing_page_and_sync_success_copy():
    dumped = json.dumps(PANEL_COPY, ensure_ascii=False)
    assert "待建讲次页" in dumped
    assert "讲次页尚未建立 · 整理完成后自动出现" in dumped
    assert "同步完成" in dumped
    assert "回到总览" in dumped


def test_student_ui_renders_all_honest_sync_states_with_actions():
    page = student_page()
    script = Path(__file__).parents[1].joinpath("pku_sync", "panel", "static", "app.js").read_text(encoding="utf-8")
    for copy in (
        "正在同步索引",
        "同步遇到问题",
        "还没有已索引的课程",
        "同步完成",
        "重新同步",
        "回到总览",
    ):
        assert copy in page
    assert 'data-state="loading"' in script
    assert 'data-state="error"' in script
    assert 'data-state="empty"' in script
    assert 'data-state="success"' in script
