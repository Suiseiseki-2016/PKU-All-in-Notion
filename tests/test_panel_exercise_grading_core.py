"""Core panel grading contract: target, preflight, settlement, and allowlists."""
from __future__ import annotations
import json
import time
from dataclasses import dataclass
from fastapi.testclient import TestClient
from pku_sync.panel.exercise_grader import GRADE_ESTIMATE_LABEL, ExerciseGrader, GradingInput
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import EXERCISE_GENERATED, PANEL_FORBIDDEN_STRINGS
from pku_sync.panel.webapi import create_app
HONEYPOTS = ("HONEYPOT题目", "HONEYPOT学生答案", "HONEYPOT解析")
@dataclass
class FakeGradeRelay:
    balance: float = 20.0
    charge: float = 2.75
    delay: float = 0.0
    def __post_init__(self): self.calls = []
    def quota(self): return {"active": True, "available": True, "llm_points_remaining": self.balance}
    def grade(self, prompt):
        self.calls.append({"operation": "grade", "prompt": prompt})
        if self.delay: time.sleep(self.delay)
        self.balance -= self.charge
        return {"content": json.dumps({"score": 88, "details": [{"question": HONEYPOTS[0], "answer": HONEYPOTS[1], "explanation": HONEYPOTS[2]}]}, ensure_ascii=False), "points_charged": self.charge}
class FakeGradeAdapter:
    def __init__(self, *, unanswered=()):
        self.unanswered = list(unanswered); self.read_calls = []; self.write_calls = []
    def read_for_grading(self, target):
        self.read_calls.append(target.page_id)
        return GradingInput(prompt="private " + " ".join(HONEYPOTS), unanswered=self.unanswered)
    def write_result(self, target, *, content, score, graded_at):
        self.write_calls.append({"target": target, "content": content, "score": score, "graded_at": graded_at})
def make_client(*, adapter=None, relay=None):
    directory = build_fake_directory(); relay = relay or FakeGradeRelay(); adapter = adapter or FakeGradeAdapter()
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    client = TestClient(create_app(directory_service=directory, platform_service=relay, grading_service=grader))
    assert client.get("/api/directory").status_code == 200
    return client, directory, relay, adapter
def wait_for_grade(client, job_id):
    for _ in range(200):
        body = client.get("/api/exercises/grade/status", params={"job_id": job_id}).json()
        if body.get("status") != "running": return body
        time.sleep(0.005)
    raise AssertionError("grade job did not finish")
def test_grade_target_is_exact_selected_page_and_course_scope():
    client, directory, relay, adapter = make_client(); directory.provider.client.calls.clear()
    started = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade"); assert started.status_code == 202
    summary = wait_for_grade(client, started.json()["job_id"]); target = adapter.write_calls[0]["target"]
    assert target.operation == "grade" and target.page_id == EXERCISE_GENERATED
    assert target.page_url.endswith(EXERCISE_GENERATED.replace("-", ""))
    assert target.course_id and target.course_title == "计算机网络" and target.scope == "考前练习"
    assert adapter.read_calls == [EXERCISE_GENERATED]
    assert len(relay.calls) == 1 and relay.calls[0]["operation"] == "grade"
    assert directory.provider.client.calls == [] and summary["status"] == "completed"
def test_missing_identity_rejected_before_notion_read_or_relay_call():
    directory = build_fake_directory("exercise-missing"); relay = FakeGradeRelay(); adapter = FakeGradeAdapter()
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    client = TestClient(create_app(directory_service=directory, platform_service=relay, grading_service=grader))
    assert client.get("/api/directory").status_code == 200; directory.provider.client.calls.clear()
    response = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert response.status_code == 409 and response.json()["status"] == "page_identity_missing"
    assert adapter.read_calls == [] and relay.calls == [] and directory.provider.client.calls == []
def test_incomplete_answers_block_with_unanswered_list_before_relay_and_keep_state():
    adapter = FakeGradeAdapter(unanswered=["第 2 题", "第 4 题"]); client, _, relay, _ = make_client(adapter=adapter)
    before = client.get("/api/exercises").json(); response = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert response.status_code == 409
    assert response.json() == {"status": "blocked", "reason": "答案尚未填写完整，请先在 Notion 完成作答。", "unanswered": ["第 2 题", "第 4 题"]}
    assert relay.calls == [] and adapter.write_calls == [] and client.get("/api/exercises").json() == before
def test_zero_answers_block_before_relay_without_charge():
    adapter = FakeGradeAdapter(unanswered=["没有可批改的答案"]); relay = FakeGradeRelay(); client, _, _, _ = make_client(adapter=adapter, relay=relay)
    before_balance = relay.balance; response = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert response.status_code == 409 and response.json()["unanswered"] == ["没有可批改的答案"]
    assert relay.calls == [] and relay.balance == before_balance
def test_summary_allowlist_settlement_balance_and_honeypot_absence():
    relay = FakeGradeRelay(balance=12.5, charge=2.75); client, _, _, adapter = make_client(relay=relay)
    started = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade").json(); summary = wait_for_grade(client, started["job_id"])
    assert set(summary) == {"status", "exercise_id", "title", "score", "graded_at", "points_charged", "points_remaining", "result_page_url"}
    assert summary["points_charged"] == relay.charge and summary["points_remaining"] == 12.5 - relay.charge == relay.balance
    assert summary["score"] == 88 and summary["graded_at"].endswith("+08:00")
    assert adapter.write_calls[0]["graded_at"] == summary["graded_at"]
    dumped = json.dumps(summary, ensure_ascii=False)
    for forbidden in (*PANEL_FORBIDDEN_STRINGS, *HONEYPOTS, "api_key", "token_count"): assert forbidden not in dumped
def test_running_payload_exposes_loader_metadata_and_rejects_double_submit():
    relay = FakeGradeRelay(delay=0.15); client, _, _, _ = make_client(relay=relay)
    first = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade"); second = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert first.status_code == 202 and first.json()["status"] == "running"
    assert first.json()["title"] == "考前练习" and first.json()["estimate"] == GRADE_ESTIMATE_LABEL
    assert second.status_code == 409 and second.json()["status"] == "blocked"
