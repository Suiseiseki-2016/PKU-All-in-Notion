from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from pku_sync.panel.exercise_grader import GradingInput, ExerciseGrader
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import EXERCISE_EXISTING, EXERCISE_GENERATED, build_panel_workspace
from pku_sync.panel.jobs import EXERCISE_JOB_BUSY_MESSAGE
from pku_sync.panel.webapi import create_app


class SlowRelay:
    def __init__(self, delay: float = 0.15):
        self.delay = delay
        self.grade_calls = 0
        self.quiz_calls = 0
        self.balance = 30.0

    def quota(self):
        return {"active": True, "available": True, "transcribe_seconds_remaining": 3600,
                "llm_points_remaining": self.balance}

    def grade(self, prompt):
        self.grade_calls += 1
        time.sleep(self.delay)
        self.balance -= 2
        return {"content": json.dumps({"score": 88}), "points_charged": 2}

    def quiz(self, prompt):
        self.quiz_calls += 1
        time.sleep(self.delay)
        self.balance -= 2
        return {"content": json.dumps({"title": "练习", "questions": []}), "points_charged": 2}


class Adapter:
    def __init__(self, *, marker=False, record=None):
        self.marker = marker
        self.record = record
        self.reads = 0
        self.writes = 0

    def read_for_grading(self, target):
        self.reads += 1
        return GradingInput(prompt="grade", unanswered=[], marker_present=self.marker,
                            existing_result=self.record)

    def write_result(self, target, *, content, score, graded_at):
        self.writes += 1
        self.marker = True

    def update_result(self, target, *, content, score, graded_at):
        self.writes += 1
        self.marker = True


def _client(*, adapter=None, relay=None):
    directory = build_fake_directory(workspace=build_panel_workspace())
    relay = relay or SlowRelay(delay=0)
    adapter = adapter or Adapter()
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    app = create_app(directory_service=directory, platform_service=relay, grading_service=grader)
    client = TestClient(app)
    assert client.get("/api/directory").status_code == 200
    return client, directory, relay, adapter


def _wait(client, job_id):
    for _ in range(200):
        body = client.get("/api/exercises/grade/status", params={"job_id": job_id}).json()
        if body.get("status") != "running":
            return body
        time.sleep(0.005)
    raise AssertionError("job did not finish")


def test_busy_payload_is_named_and_shared_by_organize_and_grade():
    relay = SlowRelay()
    client, _directory, _relay, _adapter = _client(relay=relay)
    first = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert first.status_code == 202
    second = client.post("/api/exercises/organize", json={"course_id": "course", "lecture_ids": ["lecture"]})
    assert second.status_code == 409
    body = second.json()
    assert body["code"] == "exercise_job_busy"
    assert body["message"] == EXERCISE_JOB_BUSY_MESSAGE
    assert body["reason"] == EXERCISE_JOB_BUSY_MESSAGE
    assert relay.quiz_calls == 0
    _wait(client, first.json()["job_id"])
    accepted = client.post("/api/exercises/organize", json={"course_id": "course", "lecture_ids": ["lecture"]})
    assert accepted.status_code != 409 or accepted.json().get("code") != "exercise_job_busy"


def test_marker_rerun_requires_explicit_confirmation_and_confirmed_rerun_charges():
    relay = SlowRelay(delay=0)
    adapter = Adapter(marker=True, record={"score": 90, "graded_at": "2025-01-01T00:00:00+08:00"})
    client, _directory, _relay, _adapter = _client(adapter=adapter, relay=relay)
    refused = client.post(f"/api/exercises/{EXERCISE_EXISTING}/grade")
    assert refused.status_code == 409
    assert refused.json()["status"] == "confirm_required"
    assert refused.json()["estimate"] == "预计 1–5 AI 点 · 完成后按实际用量结算"
    assert relay.grade_calls == 0
    started = client.post(f"/api/exercises/{EXERCISE_EXISTING}/grade", json={"regrade": True, "confirm": True})
    assert started.status_code == 202
    result = _wait(client, started.json()["job_id"])
    assert result["status"] == "completed"
    assert relay.grade_calls == 1


def test_directory_marker_state_remains_graded_after_answer_edits():
    workspace = build_panel_workspace()
    from pku_sync.panel.fake_workspace import paragraph

    workspace.children[EXERCISE_EXISTING] = [
        {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "批改结果"}]}},
        paragraph("答案：后来编辑的答案"),
    ]
    directory = build_fake_directory(workspace=workspace)
    client = TestClient(create_app(directory_service=directory))
    row = next(item for item in client.get("/api/directory").json()["exercises"] if item["id"] == EXERCISE_EXISTING)
    assert row["status"] == "graded"


def test_student_ui_contains_row_actions_quota_and_honest_disagreement_surfaces():
    source = Path(__file__).parents[1].joinpath("pku_sync", "panel", "static", "app.js").read_text(encoding="utf-8")
    assert "预计 1–5 AI 点" in source
    assert "重新批改" in source
    assert "confirm-regrade" in source
    assert "llm_points_remaining" in source
    assert "mismatch_notice" in source
