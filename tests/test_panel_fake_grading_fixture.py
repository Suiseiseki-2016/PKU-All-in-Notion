"""Browser-fixture coverage for the seeded fake grading flow."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from pku_sync.panel.exercise_grader import make_fake_grading_service
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import EXERCISE_GENERATED
from pku_sync.panel.platform_bridge import FakePlatformBridge
from pku_sync.panel.webapi import create_app


def test_grade_slow_variant_reaches_loading_and_metadata_only_summary():
    directory = build_fake_directory("grade-slow")
    relay = FakePlatformBridge(activated=True, llm_points=12.0, grade_delay=0.1)
    grader = make_fake_grading_service(directory, relay)
    client = TestClient(
        create_app(
            directory_service=directory,
            platform_service=relay,
            grading_service=grader,
        )
    )
    client.get("/api/directory")
    row = next(
        item for item in client.get("/api/exercises").json()["exercises"]
        if item["id"] == EXERCISE_GENERATED
    )
    assert row["status"] == "pending-grade"

    started = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    assert client.get(
        "/api/exercises/grade/status", params={"job_id": job_id}
    ).json()["status"] == "running"

    for _ in range(100):
        summary = client.get(
            "/api/exercises/grade/status", params={"job_id": job_id}
        ).json()
        if summary["status"] != "running":
            break
        time.sleep(0.005)

    assert set(summary) == {
        "status", "exercise_id", "title", "score", "graded_at",
        "points_charged", "points_remaining", "result_page_url",
    }
    assert summary["status"] == "completed"
    assert summary["points_charged"] == 3.0
    assert summary["points_remaining"] == 9.0
    assert grader.page_adapter.read_calls == [EXERCISE_GENERATED]
    assert grader.page_adapter.write_calls[0]["page_id"] == EXERCISE_GENERATED
    assert grader.page_adapter.write_calls[0]["score"] == 88
