from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pku_sync import platform
from pku_sync.agent_runner import AgentRun
from pku_sync.panel.exercise_organizer import (
    ESTIMATE_MAX_POINTS,
    ExerciseOrganizer,
    MemoryOrganizeRecordStore,
    OrganizeBlocked,
    agent_dispatch_result,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L1, build_panel_workspace
from pku_sync.panel.platform_bridge import FakePlatformBridge
from pku_sync.panel.webapi import create_app


@dataclass
class FakeRelay:
    balance: float = 20.0
    points_charged: float = 3.0
    status: int = 200

    def __post_init__(self):
        self.calls: list[dict] = []

    def quota(self) -> dict:
        return {"active": True, "available": True, "llm_points_remaining": self.balance,
                "transcribe_seconds_remaining": 3600}

    def quiz(self, prompt: str) -> dict:
        self.calls.append({"operation": "quiz", "prompt": prompt})
        if self.status != 200:
            raise OrganizeBlocked.from_relay_status(self.status)
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
        return {"id": "2c000001-0000-4000-8000-000000000901",
                "url": "https://www.notion.so/2c000001000040008000000000000901",
                "last_edited_time": "2026-09-19T08:00:00.000Z"}


class FakeNotes:
    def __init__(self, ready: bool = True):
        self.ready = ready

    def for_scope(self, *, course_title: str, lecture_titles: list[str]) -> list[dict]:
        if not self.ready:
            return []
        return [{"title": lecture_titles[0], "source": "课堂录像笔记", "notes": "私有笔记正文"}]


def make_client(*, relay=None, adapter=None, notes=None, dispatcher=None):
    directory = build_fake_directory(workspace=build_panel_workspace())
    relay = relay or FakeRelay()
    adapter = adapter or FakePageAdapter()
    organizer = ExerciseOrganizer(directory_service=directory, relay=relay,
        page_adapter=adapter, notes_provider=notes or FakeNotes(),
        record_store=MemoryOrganizeRecordStore(), agent_dispatcher=dispatcher)
    platform_bridge = FakePlatformBridge(activated=True, llm_points=relay.balance)
    app = create_app(directory_service=directory, platform_service=platform_bridge,
                     organizer_service=organizer)
    return TestClient(app), directory, relay, adapter


def organize_payload(client: TestClient, *, course_id=COURSE_NET, lecture_ids=None):
    started = client.post("/api/exercises/organize", json={
        "course_id": course_id,
        "lecture_ids": [NET_L1] if lecture_ids is None else lecture_ids,
    })
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    for _ in range(200):
        result = client.get("/api/exercises/organize/status", params={"job_id": job_id})
        if result.json().get("status") != "running":
            return result
        time.sleep(0.005)
    pytest.fail("organize job did not finish")


def organize(client: TestClient):
    return organize_payload(client)


def block_text(block: dict) -> str:
    kind = block["type"]
    return "".join(part.get("text", {}).get("content", "")
                   for part in block[kind].get("rich_text", []))


def test_estimate_is_static_and_no_organize_call_precedes_confirm():
    client, _, relay, adapter = make_client()
    estimate = client.get("/api/exercises/organize/estimate")
    assert estimate.status_code == 200
    assert estimate.json() == {"label": "预计 1–5 AI 点 · 完成后按实际用量结算",
                               "min_points": 1, "max_points": ESTIMATE_MAX_POINTS}
    assert relay.calls == [] and adapter.calls == []


def test_confirmed_organize_creates_under_course_with_mixed_questions_and_settlement():
    client, _, relay, adapter = make_client()
    before = relay.balance
    response = organize(client)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "exercise_id", "url", "title", "state",
                         "points_charged", "points_remaining", "reused"}
    assert body["status"] == "completed" and body["state"] == "organized"
    assert body["points_charged"] == 3.0
    assert body["points_remaining"] == before - 3.0
    assert len(relay.calls) == 1 and relay.calls[0]["operation"] == "quiz"
    assert "私有笔记正文" in relay.calls[0]["prompt"]
    create = adapter.calls[0]
    assert create["parent"] == COURSE_NET
    assert create["parent"] != "2c000001-0000-4000-8000-000000000020"
    texts = [block_text(block) for block in create["children"]]
    types = [kind for kind in ("选择", "判断", "填空", "简答", "论述")
             if any(kind in text for text in texts)]
    assert 3 <= len(types) <= 5
    assert sum("来源：" in text for text in texts) == 5
    teacher_at = texts.index("教师区（答案）")
    assert not any("答案：" in text for text in texts[:teacher_at])
    assert sum("答案：" in text for text in texts[teacher_at + 1:]) == 5
    rows = client.get("/api/exercises").json()["exercises"]
    assert next(row for row in rows if row["id"] == body["exercise_id"])["status_label"] == "已整理"


def test_same_scope_reuses_identity_without_second_create_or_charge():
    client, _, relay, adapter = make_client()
    first = organize(client).json()
    second = organize(client).json()
    assert second["exercise_id"] == first["exercise_id"] and second["url"] == first["url"]
    assert second["reused"] is True and second["points_charged"] == 0
    assert len(adapter.calls) == 1 and len(relay.calls) == 1


def test_insufficient_points_blocks_before_agent_relay_or_create():
    relay = FakeRelay(balance=4.0)
    dispatched: list[dict] = []
    client, _, _, adapter = make_client(relay=relay,
        dispatcher=lambda target: dispatched.append(target))
    response = organize(client)
    assert response.status_code == 200 and response.json()["status"] == "blocked"
    assert "AI 点不足" in response.json()["reason"]
    assert "points_charged" not in response.json()
    assert relay.calls == [] and adapter.calls == [] and dispatched == []


@pytest.mark.parametrize("status", [401, 402, 502, 503])
def test_relay_failures_are_blocked_without_row_or_settlement(status):
    relay = FakeRelay(status=status)
    client, _, _, adapter = make_client(relay=relay)
    before_rows = client.get("/api/exercises").json()["exercises"]
    response = organize(client)
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "blocked" and body["reason"] and body["retryable"] is True
    assert "points_charged" not in body and adapter.calls == []
    assert client.get("/api/exercises").json()["exercises"] == before_rows


def test_empty_scope_and_no_notes_and_unknown_course_block_before_relay():
    client, _, relay, _ = make_client()
    empty = organize_payload(client, lecture_ids=[])
    unknown = organize_payload(client, course_id="missing")
    assert empty.json()["status"] == "blocked"
    assert unknown.json()["status"] == "blocked"
    assert relay.calls == []
    no_notes_client, _, no_notes_relay, _ = make_client(notes=FakeNotes(ready=False))
    no_notes = organize(no_notes_client)
    assert "可用课堂笔记" in no_notes.json()["reason"]
    assert no_notes_relay.calls == []


@pytest.mark.parametrize(("run", "reason"), [
    (AgentRun("factory", Path("x"), 0, output="TUI_RESULT=blocked reason=课程映射失效"), "课程映射失效"),
    (AgentRun("factory", Path("x"), 7, stderr="agent unavailable", output=""), "agent unavailable"),
    (AgentRun("factory", Path("x"), 0, output="ordinary output without marker"), "缺少 TUI_RESULT"),
])
def test_agent_protocol_blocked_nonzero_and_missing_marker_keep_original_reason(run, reason):
    result = agent_dispatch_result(run)
    assert result.status == "blocked" and reason in result.reason


def test_dispatched_agent_block_prevents_relay_create_and_settlement():
    blocked = AgentRun("factory", Path("x"), 0,
        output="progress\nTUI_RESULT=blocked reason=所选讲次没有完成笔记")
    relay = FakeRelay()
    client, _, _, adapter = make_client(relay=relay, dispatcher=lambda target: blocked)
    response = organize(client)
    assert response.json() == {"status": "blocked", "reason": "所选讲次没有完成笔记",
                               "retryable": True}
    assert relay.calls == [] and adapter.calls == []


def test_second_trigger_is_rejected_while_organize_job_is_running():
    class WaitingOrganizer:
        def estimate(self):
            return {"label": "estimate", "min_points": 1, "max_points": 5}

        def organize(self, course_id, lecture_ids):
            time.sleep(0.15)
            return {"status": "completed", "exercise_id": "one"}

    client = TestClient(create_app(organizer_service=WaitingOrganizer()))
    payload = {"course_id": COURSE_NET, "lecture_ids": [NET_L1]}
    first = client.post("/api/exercises/organize", json=payload)
    second = client.post("/api/exercises/organize", json=payload)
    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["status"] == "blocked"


def test_relay_llm_uses_existing_operation_system_user_contract(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"content": "{}", "points_charged": 1.25}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(platform.httpx, "post", fake_post)
    settings = SimpleNamespace(platform_token="session-token",
        cloud_transcribe_url="https://relay.example/v1/transcribe", llm_timeout=30)
    result = platform.llm("quiz", "PROMPT", settings)
    assert set(captured["json"]) == {"operation", "system", "user"}
    assert captured["json"]["operation"] == "quiz"
    assert captured["json"]["user"] == "PROMPT"
    assert result["points_charged"] == 1.25
@pytest.mark.parametrize("output", [
    "TUI_RESULT=success\nTUI_RESULT=blocked reason=late failure",
    "TUI_RESULT=success\nTUI_RESULT=success",
])
def test_agent_protocol_rejects_multiple_markers(output):
    result = agent_dispatch_result(AgentRun("factory", Path("x"), 0, output=output))
    assert result.status == "blocked"
    assert "多个" in result.reason


def test_generated_title_is_normalized_to_recognized_exercise_vocabulary():
    relay = FakeRelay()
    original_quiz = relay.quiz

    def quiz_without_exercise_marker(prompt):
        result = original_quiz(prompt)
        payload = json.loads(result["content"])
        payload["title"] = "第一讲知识检测"
        result["content"] = json.dumps(payload, ensure_ascii=False)
        return result

    relay.quiz = quiz_without_exercise_marker
    client, _, _, adapter = make_client(relay=relay)
    response = organize(client)
    assert response.json()["title"].startswith("练习 · ")
    assert adapter.calls[0]["title"] == response.json()["title"]


def test_status_endpoint_requires_matching_job_identity():
    class WaitingOrganizer:
        def estimate(self):
            return {"label": "estimate", "min_points": 1, "max_points": 5}

        def organize(self, course_id, lecture_ids):
            time.sleep(0.03)
            return {"status": "completed", "exercise_id": course_id}

    client = TestClient(create_app(organizer_service=WaitingOrganizer()))
    started = client.post("/api/exercises/organize", json={
        "course_id": COURSE_NET, "lecture_ids": [NET_L1]
    }).json()
    missing = client.get("/api/exercises/organize/status")
    wrong = client.get("/api/exercises/organize/status", params={"job_id": "wrong"})
    assert missing.status_code == 422
    assert wrong.status_code == 404
    matched = client.get("/api/exercises/organize/status", params={"job_id": started["job_id"]})
    assert matched.status_code == 200
