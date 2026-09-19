"""Lecture/material launch recovery coverage (additive M3 feature tests)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L2
from pku_sync.panel.student_ui import student_page
from pku_sync.panel.webapi import create_app


def make_client(variant: str = "missing-lecture") -> TestClient:
    return TestClient(create_app(directory_service=build_fake_directory(variant=variant)))


def test_missing_lecture_page_is_visible_and_keeps_course_fallback():
    client = make_client()

    directory = client.get("/api/directory")
    assert directory.status_code == 200
    course = next(item for item in directory.json()["courses"] if item["id"] == COURSE_NET)
    lecture = next(item for item in course["lectures"] if item["id"] == NET_L2)
    assert lecture["page_state"] == "missing"
    assert lecture["url"] == ""

    response = client.post(
        "/api/launch",
        json={"target_id": NET_L2, "target_type": "lecture", "course_id": COURSE_NET},
    )
    assert response.status_code == 404
    assert response.json() == {
        "detail": "该条目还没有对应的 Notion 页面，可以先用课程页打开。",
        "status": "missing_mapping",
        "target_id": NET_L2,
        "url": None,
        "fallback": {
            "id": COURSE_NET,
            "url": f"https://www.notion.so/{COURSE_NET.replace('-', '')}",
            "title": "计算机网络",
        },
    }


def test_missing_lecture_launch_never_searches_or_fabricates_url():
    client = make_client()
    client.get("/api/directory")
    service = client.app.state.directory_service
    before = list(service.provider.client.calls)
    body = client.post(
        "/api/launch",
        json={"target_id": NET_L2, "course_id": COURSE_NET},
    ).json()
    assert body["url"] is None
    assert service.provider.client.calls == before
    assert "notion.so/" not in json.dumps(body["url"])


def test_lecture_renderer_disables_missing_launch_and_catches_transport_failure():
    page = student_page()
    script = Path(__file__).parents[1].joinpath(
        "pku_sync", "panel", "static", "app.js"
    ).read_text(encoding="utf-8")

    assert "待建讲次页" in page
    assert "讲次页尚未建立 · 整理完成后自动出现" in page
    assert 'page_state === "missing"' in script
    assert 'disabled: lecture.page_state === "missing"' in script
    assert ".catch(function ()" in script
    assert 'status: "failed"' in script
    assert "重新打开" in page
