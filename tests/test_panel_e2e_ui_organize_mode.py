"""VAL-PKG-011 (pkg-fresh-journey): the packaged student UI's organize runs in
[E2E] mode on an EXERCISE_UI_E2E_MODE panel instance.

The attended fresh-machine journey has the USER drive organize through the
packaged student SPA. The SPA's confirm sends ``{course_id, lecture_ids}``
with no ``e2e_mode`` field, while the mission's Notion-mutation boundary
allows creating pages only when they carry the ``[E2E]`` title prefix. The
scratch/test-instance setting ``EXERCISE_UI_E2E_MODE`` (env-driven via
``exercise_ui_e2e_mode``; false on every real install) makes the panel's
organize route run such UI-driven requests in e2e mode: the created exercise
page title carries exactly one ``[E2E]`` prefix and the record lands under
the e2e scope key (idempotent reruns reuse it with zero charge).

Coexistence is pinned too: the request-level ``e2e_mode`` semantics are
unchanged (explicit e2e_mode still requires the organizer's
``EXERCISE_E2E_ENABLED`` gate), and an E2E-enabled organizer WITHOUT
``EXERCISE_UI_E2E_MODE`` keeps UI-shaped requests in normal mode with a
normal title — the behavior pinned by the mission's json-fence/surrogate
payload tests.

Additive coverage for feature pkg-fresh-journey; all fakes are local to
this file (patterned after tests/test_panel_exercise_organize_flow.py,
never importing from it).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pku_sync.panel.exercise_organizer import (
    E2E_TITLE_PREFIX,
    ExerciseOrganizer,
    MemoryOrganizeRecordStore,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L1, build_panel_workspace
from pku_sync.panel.platform_bridge import FakePlatformBridge
from pku_sync.panel.webapi import create_app


@dataclass
class FakeRelay:
    balance: float = 20.0
    points_charged: float = 3.0
    calls: list = field(default_factory=list)

    def quota(self) -> dict:
        return {"active": True, "available": True,
                "llm_points_remaining": self.balance,
                "transcribe_seconds_remaining": 3600}

    def quiz(self, prompt: str) -> dict:
        self.calls.append({"operation": "quiz", "prompt": prompt})
        self.balance -= self.points_charged
        return {"content": json.dumps({"title": "第一讲 · 计算机网络练习", "questions": [
            {"type": "选择", "question": "选择题", "source": "第一讲", "answer": "A"},
            {"type": "判断", "question": "判断题", "source": "第一讲", "answer": "正确"},
            {"type": "填空", "question": "填空题", "source": "第一讲", "answer": "分层"},
            {"type": "简答", "question": "简答题", "source": "第一讲", "answer": "端到端"},
            {"type": "论述", "question": "论述题", "source": "第一讲", "answer": "按层分析"},
        ]}, ensure_ascii=False), "points_charged": self.points_charged}


class FakePageAdapter:
    def __init__(self):
        self.calls: list[dict] = []

    def create_page(self, parent_page_id: str, title: str, *, children: list[dict]):
        self.calls.append({"parent": parent_page_id, "title": title, "children": children})
        return {"id": "2c000001-0000-4000-8000-0000000009f1",
                "url": "https://www.notion.so/2c0000010000400080000000000009f1",
                "last_edited_time": "2026-09-23T08:00:00.000Z"}


class FakeNotes:
    def for_scope(self, *, course_title: str, lecture_titles: list[str]) -> list[dict]:
        return [{"title": lecture_titles[0], "source": "课堂录像笔记", "notes": "私有笔记正文"}]


def make_client(tmp_path, *, e2e_enabled: bool, ui_e2e_mode: bool):
    directory = build_fake_directory(workspace=build_panel_workspace())
    relay = FakeRelay()
    adapter = FakePageAdapter()
    organizer = ExerciseOrganizer(
        directory_service=directory, relay=relay, page_adapter=adapter,
        notes_provider=FakeNotes(), record_store=MemoryOrganizeRecordStore(),
        e2e_enabled=e2e_enabled)
    bridge = FakePlatformBridge(activated=True, llm_points=relay.balance)
    settings = SimpleNamespace(data_dir=tmp_path,
                               exercise_ui_e2e_mode=ui_e2e_mode)
    app = create_app(settings=settings, directory_service=directory,
                     platform_service=bridge, organizer_service=organizer)
    return TestClient(app), relay, adapter


def ui_organize(client: TestClient, *, e2e_mode=None):
    """The student SPA's confirm payload: course + lectures only (no
    e2e_mode), unless the test pins an explicit e2e_mode value."""
    payload = {"course_id": COURSE_NET, "lecture_ids": [NET_L1]}
    if e2e_mode is not None:
        payload["e2e_mode"] = e2e_mode
    started = client.post("/api/exercises/organize", json=payload)
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    for _ in range(200):
        result = client.get("/api/exercises/organize/status", params={"job_id": job_id})
        if result.json().get("status") != "running":
            return result
        time.sleep(0.005)
    pytest.fail("organize job did not finish")


def test_ui_shaped_organize_on_ui_e2e_instance_creates_e2e_titled_page(tmp_path):
    client, relay, adapter = make_client(tmp_path, e2e_enabled=True, ui_e2e_mode=True)
    before = relay.balance
    response = ui_organize(client)
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "completed" and body["state"] == "organized"
    # The created page title carries exactly one [E2E] prefix (never
    # doubled): the user-driven journey's only workspace mutation is
    # [E2E]-titled, which is what the mission's mutation boundary requires.
    assert body["title"].startswith(E2E_TITLE_PREFIX)
    assert body["title"].count("[E2E]") == 1
    assert adapter.calls[0]["title"] == body["title"]
    assert adapter.calls[0]["parent"] == COURSE_NET
    assert body["points_charged"] == 3.0 and body["points_remaining"] == before - 3.0
    # The directory row for the [E2E] page is visible with its state label.
    rows = client.get("/api/exercises").json()["exercises"]
    row = next(item for item in rows if item["id"] == body["exercise_id"])
    assert row["title"] == body["title"] and row["status_label"] == "已整理"


def test_ui_e2e_instance_rerun_reuses_identity_with_zero_charge(tmp_path):
    client, relay, adapter = make_client(tmp_path, e2e_enabled=True, ui_e2e_mode=True)
    first = ui_organize(client).json()
    second = ui_organize(client).json()
    assert second["exercise_id"] == first["exercise_id"] and second["reused"] is True
    assert second["points_charged"] == 0
    assert len(adapter.calls) == 1 and len(relay.calls) == 1


def test_default_instance_keeps_normal_title_for_ui_shaped_request(tmp_path):
    client, _, adapter = make_client(tmp_path, e2e_enabled=False, ui_e2e_mode=False)
    body = ui_organize(client).json()
    assert body["status"] == "completed"
    assert not body["title"].startswith(E2E_TITLE_PREFIX)
    assert "[E2E]" not in body["title"]
    assert adapter.calls[0]["title"] == body["title"]


def test_e2e_enabled_organizer_without_ui_flag_keeps_ui_requests_normal(tmp_path):
    """The EXERCISE_E2E_ENABLED gate alone never flips UI-driven requests:
    the request-level e2e_mode semantics pinned by the json-fence/surrogate
    payload tests stay byte-identical (normal titles, normal payloads)."""
    client, _, adapter = make_client(tmp_path, e2e_enabled=True, ui_e2e_mode=False)
    body = ui_organize(client).json()
    assert body["status"] == "completed"
    assert not body["title"].startswith(E2E_TITLE_PREFIX)
    assert adapter.calls[0]["title"] == body["title"]


def test_ui_e2e_flag_alone_cannot_bypass_the_organizer_e2e_gate(tmp_path):
    client, relay, adapter = make_client(tmp_path, e2e_enabled=False, ui_e2e_mode=True)
    response = ui_organize(client)
    body = response.json()
    assert body["status"] == "blocked" and body["retryable"] is True
    assert "E2E 练习整理模式未启用" in body["reason"]
    assert relay.calls == [] and adapter.calls == []


def test_explicit_e2e_request_still_gated_when_flag_off(tmp_path):
    client, relay, adapter = make_client(tmp_path, e2e_enabled=False, ui_e2e_mode=False)
    response = ui_organize(client, e2e_mode=True)
    body = response.json()
    assert body["status"] == "blocked" and body["retryable"] is True
    assert "E2E 练习整理模式未启用" in body["reason"]
    assert relay.calls == [] and adapter.calls == []
