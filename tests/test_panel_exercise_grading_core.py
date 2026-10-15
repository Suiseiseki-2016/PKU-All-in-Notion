"""Core panel grading contract: target, preflight, settlement, and allowlists."""
from __future__ import annotations
import json
import time
from dataclasses import dataclass
from fastapi.testclient import TestClient
from pku_sync.panel.quiz_grading_core import ExerciseGrader, GradingInput, GradeBlocked

from grading_workspace import EXERCISE_GENERATED, PANEL_FORBIDDEN_STRINGS
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
    directory = SimpleNamespace(local_grading_records={}, exercise_grade_target=lambda _: {"page_id": EXERCISE_GENERATED, "page_url": "https://www.notion.so/" + EXERCISE_GENERATED.replace("-", ""), "title": "考前练习", "course_id": "course-page", "course_title": "计算机网络", "scope": "考前练习"})
    relay = relay or FakeGradeRelay()
    adapter = adapter or FakeGradeAdapter()
    return ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter), directory, relay, adapter
def wait_for_grade(client, job_id):
    for _ in range(200):
        body = client.get("/api/exercises/grade/status", params={"job_id": job_id}).json()
        if body.get("status") != "running": return body
        time.sleep(0.005)
    raise AssertionError("grade job did not finish")
def test_grade_target_is_exact_selected_page_and_course_scope():
    grader, directory, relay, adapter = make_client()
    prepared = grader.prepare(EXERCISE_GENERATED)
    target = prepared[0]
    assert target.operation == "grade" and target.page_id == EXERCISE_GENERATED
    assert target.page_url.endswith(EXERCISE_GENERATED.replace("-", ""))
    assert target.course_id == "course-page" and target.course_title == "计算机网络" and target.scope == "考前练习"
    summary = grader.grade(prepared)
    assert adapter.read_calls == [EXERCISE_GENERATED]
    assert len(relay.calls) == len(adapter.write_calls) == 1 and summary["status"] == "completed"
def test_missing_identity_rejected_before_notion_read_or_relay_call():
    grader, directory, relay, adapter = make_client()
    directory.exercise_grade_target = lambda _: None
    with pytest.raises(GradeBlocked): grader.prepare(EXERCISE_GENERATED)
    assert adapter.read_calls == [] and relay.calls == [] and adapter.write_calls == []
def test_incomplete_answers_block_with_unanswered_list_before_relay_and_keep_state():
    grader, directory, relay, adapter = make_client(adapter=FakeGradeAdapter(unanswered=["第 2 题", "第 4 题"]))
    with pytest.raises(GradeBlocked) as blocked: grader.prepare(EXERCISE_GENERATED)
    assert blocked.value.unanswered == ["第 2 题", "第 4 题"]
    assert relay.calls == [] and adapter.write_calls == [] and directory.local_grading_records == {}
def test_zero_answers_block_before_relay_without_charge():
    grader, _, relay, adapter = make_client(adapter=FakeGradeAdapter(unanswered=["没有可批改的答案"]))
    balance = relay.balance
    with pytest.raises(GradeBlocked) as blocked: grader.prepare(EXERCISE_GENERATED)
    assert blocked.value.unanswered == ["没有可批改的答案"]
    assert relay.calls == [] and relay.balance == balance
def test_summary_allowlist_settlement_balance_and_honeypot_absence():
    grader, _, relay, adapter = make_client(relay=FakeGradeRelay(balance=12.5, charge=2.75))
    summary = grader.grade(grader.prepare(EXERCISE_GENERATED))
    assert set(summary) == {"status", "exercise_id", "title", "score", "graded_at", "points_charged", "points_remaining", "result_page_url"}
    assert summary["points_charged"] == relay.charge and summary["points_remaining"] == 12.5 - relay.charge == relay.balance
    assert summary["score"] == 88 and summary["graded_at"].endswith("+08:00")
    assert adapter.write_calls[0]["graded_at"] == summary["graded_at"]
    dumped = json.dumps(summary, ensure_ascii=False)
    for forbidden in (*PANEL_FORBIDDEN_STRINGS, *HONEYPOTS, "api_key", "token_count"): assert forbidden not in dumped
def test_running_gate_rejects_double_submit_and_releases_after_completion():
    gate = ExerciseJobGate()
    assert gate.acquire("campus-grade")
    assert gate.current() == "campus-grade"
    assert not gate.acquire("campus-grade")
    assert not gate.acquire("campus-quiz")
    gate.release("campus-quiz")
    assert gate.current() == "campus-grade"
    gate.release("campus-grade")
    assert gate.current() is None and gate.acquire("campus-grade")

import pytest
from types import SimpleNamespace
from pku_sync.panel.jobs import ExerciseJobGate
