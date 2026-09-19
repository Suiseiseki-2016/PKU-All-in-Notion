"""Exercise-specific identity launch actions (VAL-EXER-011/012/013/018)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import COURSE_NET, EXERCISE_EXISTING, EXERCISE_GENERATED
from pku_sync.panel.webapi import create_app


APP_JS = Path(__file__).parents[1] / "pku_sync" / "panel" / "static" / "app.js"


def make_client(variant: str = "normal"):
    service = build_fake_directory(variant=variant)
    return TestClient(create_app(directory_service=service)), service


def row_by_id(client: TestClient, exercise_id: str) -> dict:
    response = client.get("/api/directory")
    assert response.status_code == 200
    return next(row for row in response.json()["exercises"] if row["id"] == exercise_id)


def test_answer_launch_opens_exact_stored_identity_without_any_adapter_lookup():
    client, service = make_client()
    row = row_by_id(client, EXERCISE_GENERATED)
    service.provider.client.calls.clear()
    response = client.post(f"/api/exercises/{EXERCISE_GENERATED}/launch", json={"purpose": "answer"})
    assert response.status_code == 200
    assert response.json() == {"status": "opened", "target_id": EXERCISE_GENERATED, "url": row["url"], "fallback": None}
    assert service.provider.client.calls == []
    assert service.exercise_events.launched_ids() == {EXERCISE_GENERATED}


def test_failed_answer_launch_is_retryable_and_does_not_change_directory_or_records():
    client, service = make_client("exercise-fault-launch")
    before = row_by_id(client, EXERCISE_GENERATED)
    service.provider.client.calls.clear()
    failed = client.post(f"/api/exercises/{EXERCISE_GENERATED}/launch", json={"purpose": "answer"})
    assert failed.status_code == 502
    assert failed.json() == {"detail": "打开没有成功，请稍后重试。"}
    assert service.exercise_events.launched_ids() == set()
    assert service.provider.client.calls == []
    assert row_by_id(client, EXERCISE_GENERATED) == before
    retried = client.post(f"/api/exercises/{EXERCISE_GENERATED}/launch", json={"purpose": "answer"})
    assert retried.status_code == 200
    assert retried.json()["url"] == before["url"]
    assert service.exercise_events.launched_ids() == {EXERCISE_GENERATED}


def test_missing_exercise_identity_blocks_launch_and_grade_with_course_fallback():
    client, service = make_client("exercise-missing")
    row = row_by_id(client, EXERCISE_GENERATED)
    assert row["url"] == ""
    service.provider.client.calls.clear()
    launch = client.post(f"/api/exercises/{EXERCISE_GENERATED}/launch", json={"purpose": "answer"})
    grade = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert launch.status_code == 404
    assert launch.json()["status"] == "page_identity_missing"
    assert launch.json()["detail"] == "该练习还没有对应的 Notion 页面，暂时无法作答或批改。可以先打开课程页查看。"
    assert launch.json()["url"] is None
    assert launch.json()["fallback"]["id"] == COURSE_NET
    assert grade.status_code == 409
    assert grade.json()["status"] == "page_identity_missing"
    assert grade.json()["reason"]
    assert grade.json()["fallback"]["id"] == COURSE_NET
    assert service.exercise_events.launched_ids() == set()
    assert service.provider.client.calls == []


def test_grade_identity_guard_preserves_transient_launch_failure():
    client, service = make_client("exercise-fault-launch")
    row_by_id(client, EXERCISE_GENERATED)
    service.provider.client.calls.clear()
    response = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert response.status_code == 502
    assert response.json() == {
        "status": "failed",
        "reason": "打开没有成功，请稍后重试。",
        "fallback": None,
    }
    assert service.exercise_events.launched_ids() == set()
    assert service.provider.client.calls == []

def test_result_launch_opens_graded_exercise_writeback_identity_without_marking_answer_started():
    client, service = make_client()
    row = row_by_id(client, EXERCISE_EXISTING)
    assert row["status"] == "graded"
    service.provider.client.calls.clear()
    response = client.post(f"/api/exercises/{EXERCISE_EXISTING}/launch", json={"purpose": "result"})
    assert response.status_code == 200
    assert response.json()["target_id"] == EXERCISE_EXISTING
    assert response.json()["url"] == row["url"]
    assert service.exercise_events.launched_ids() == set()
    assert service.provider.client.calls == []


def test_exercise_ui_uses_purpose_specific_launch_and_missing_mapping_controls():
    script = APP_JS.read_text(encoding="utf-8")
    assert '"answer"' in script
    assert '"result"' in script
    assert "page_identity_missing" in script
    assert "retry-exercise-launch" in script
    assert "launch-exercise-fallback" in script
    assert "result.body.fallback.url" in script
    assert "course.title === row.course" not in script
    assert "window.location.assign(result.body.url)" in script
