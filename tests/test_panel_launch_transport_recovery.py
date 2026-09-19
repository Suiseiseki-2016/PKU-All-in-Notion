"""Transport-level launch recovery coverage (additive M3 feature tests)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import NET_L1
from pku_sync.panel.student_ui import student_page
from pku_sync.panel.webapi import create_app

def test_transport_launch_failure_is_safe_and_retry_reuses_target_identity():
    client = TestClient(create_app(directory_service=build_fake_directory(variant="transport-launch")))
    assert client.get("/api/directory").status_code == 200
    failed = client.post("/api/launch", json={"target_id": NET_L1})
    assert failed.status_code == 502
    assert failed.json() == {"detail": "打开没有成功，请稍后重试。"}
    retried = client.post("/api/launch", json={"target_id": NET_L1})
    assert retried.status_code == 200
    assert retried.json()["status"] == "opened"
    assert retried.json()["target_id"] == NET_L1
    assert retried.json()["url"] == f"https://www.notion.so/{NET_L1.replace('-', '')}"

def test_launch_recovery_copy_is_panel_copy_driven():
    page = student_page()
    assert '"missing_title": "待建讲次页"' in page
    assert '"missing_body": "讲次页尚未建立 · 整理完成后自动出现"' in page
    assert '"fallback": "查看课程页"' in page
