"""Additive variant-wiring coverage for the fake blocked/error UI fixtures.

The three browser-verification variants added for the M3 blocked-path
assertions — VAL-EXER-009 organize-blocked, VAL-EXER-023 low-balance 402,
VAL-EXER-024 grading 502 — are wired through the SAME factory the ``--fake``
CLI uses (``fake_panel.build_fake_panel_services``). These tests pin the exact
variant wiring a validator starts: the directory loads on every variant,
blocked/error payloads carry a reason and never a success/score/settlement
surface, 402/502 leave zero Notion writes and no settlement, and the one-shot
502 retry completes exactly one result set. The failure-injection knobs are
proven inert when no variant opts into them.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from pku_sync.panel.fake_panel import build_fake_panel_services
from pku_sync.panel.fake_workspace import COURSE_NET, EXERCISE_GENERATED, NET_L1
from pku_sync.panel.webapi import create_app

# The graded-state keys that must never appear in a blocked/error payload.
_SUCCESS_FIELDS = {"score", "points_charged", "points_remaining",
                   "result_page_url", "graded_at"}

SLOW_VARIANTS = ["grade-slow", "grade-recovery"]


def make_variant_app(variant):
    """The app exactly as ``pku-sync panel --fake --fake-variant <variant>``."""
    (directory, connection, platform,
     organizer, grading) = build_fake_panel_services(variant)
    app = create_app(
        directory_service=directory,
        connection_service=connection,
        platform_service=platform,
        organizer_service=organizer,
        grading_service=grading,
    )
    return TestClient(app), directory, platform, organizer, grading


def wait_for_grade(client: TestClient, job_id: str, *, attempts: int = 400) -> dict:
    for _ in range(attempts):
        result = client.get("/api/exercises/grade/status",
                            params={"job_id": job_id}).json()
        if result.get("status") != "running":
            return result
        time.sleep(0.005)
    pytest.fail("grade job did not finish")


def wait_for_organize(client: TestClient, job_id: str, *, attempts: int = 400) -> dict:
    for _ in range(attempts):
        result = client.get("/api/exercises/organize/status",
                            params={"job_id": job_id}).json()
        if result.get("status") != "running":
            return result
        time.sleep(0.005)
    pytest.fail("organize job did not finish")


def exercise_row(client: TestClient, exercise_id: str) -> dict:
    return next(
        item for item in client.get("/api/exercises").json()["exercises"]
        if item["id"] == exercise_id
    )


@pytest.mark.parametrize("variant", ["organize-blocked", "low-balance", "grade-502"])
def test_each_blocked_variant_loads_directory_and_exercise_rows(variant):
    client, _directory, _platform, _organizer, _grading = make_variant_app(variant)
    assert client.get("/api/directory").status_code == 200
    payload = client.get("/api/exercises")
    assert payload.status_code == 200
    ids = {row["id"] for row in payload.json()["exercises"]}
    assert EXERCISE_GENERATED in ids


def test_low_balance_and_grade_502_variants_seed_the_generated_exercise_pending_grade():
    for variant in ("low-balance", "grade-502"):
        client, _directory, _platform, _organizer, _grading = make_variant_app(variant)
        row = exercise_row(client, EXERCISE_GENERATED)
        assert row["status"] == "pending-grade"
        assert row["status_label"] == "待批改"


def test_organize_blocked_variant_returns_502_blocked_with_reason_no_row_no_settlement():
    client, _directory, platform, _organizer, _grading = make_variant_app("organize-blocked")
    assert client.get("/api/directory").status_code == 200
    before_ids = {row["id"] for row in client.get("/api/exercises").json()["exercises"]}

    started = client.post("/api/exercises/organize", json={
        "course_id": COURSE_NET, "lecture_ids": [NET_L1],
    })
    assert started.status_code == 202
    result = wait_for_organize(client, started.json()["job_id"])

    assert result["status"] == "blocked"
    assert "稍后重试" in result["reason"]
    assert result.get("retryable") is True
    # blocked payload carries a reason and no success/settlement surface
    assert "exercise_id" not in result
    assert not (_SUCCESS_FIELDS & set(result))

    # directory gains no row and nothing was created or settled
    after_ids = {row["id"] for row in client.get("/api/exercises").json()["exercises"]}
    assert after_ids == before_ids
    assert platform.llm_calls == [{"operation": "quiz"}]
    assert platform.quota()["llm_points_remaining"] == 100.0


def test_low_balance_variant_grade_returns_402_quota_blocked_zero_writes():
    client, _directory, platform, _organizer, grading = make_variant_app("low-balance")
    assert exercise_row(client, EXERCISE_GENERATED)["status"] == "pending-grade"
    # the 4-point balance is kept and surfaced
    quota = client.get("/api/platform/quota").json()
    assert quota["llm_points_remaining"] == 4.0

    started = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert started.status_code == 202
    result = wait_for_grade(client, started.json()["job_id"])

    assert result["status"] == "blocked"
    assert "兑换新额度" in result["reason"]
    # no score / settlement on the blocked payload
    assert not (_SUCCESS_FIELDS & set(result))
    # zero Notion writes, no balance change, row stays 待批改
    assert grading.page_adapter.write_calls == []
    assert platform.quota()["llm_points_remaining"] == 4.0
    assert exercise_row(client, EXERCISE_GENERATED)["status"] == "pending-grade"
    assert platform.llm_calls == [{"operation": "grade"}]


def test_grade_502_variant_first_attempt_retryable_then_exactly_one_result():
    client, _directory, platform, _organizer, grading = make_variant_app("grade-502")
    assert exercise_row(client, EXERCISE_GENERATED)["status"] == "pending-grade"

    first = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert first.status_code == 202
    failed = wait_for_grade(client, first.json()["job_id"])

    # retryable error state: no summary, no write
    assert failed["status"] == "failed"
    assert failed.get("retryable") is True
    assert "稍后重试" in failed["reason"]
    assert not (_SUCCESS_FIELDS & set(failed))
    assert grading.page_adapter.write_calls == []

    # ordinary retry through the same API action completes exactly one result
    second = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert second.status_code == 202
    completed = wait_for_grade(client, second.json()["job_id"])
    assert completed["status"] == "completed"
    assert set(completed) == {
        "status", "exercise_id", "title", "score", "graded_at",
        "points_charged", "points_remaining", "result_page_url",
    }
    assert completed["score"] == 88
    assert completed["points_charged"] == 3.0
    assert completed["points_remaining"] == 97.0
    assert len(grading.page_adapter.write_calls) == 1
    assert grading.page_adapter.write_calls[0]["page_id"] == EXERCISE_GENERATED
    assert platform.llm_calls == [{"operation": "grade"}, {"operation": "grade"}]


def test_failure_injection_knobs_are_inert_in_normal_fake_mode():
    client, _directory, platform, _organizer, grading = make_variant_app("normal")
    # the app loads its directory exactly like the browser does on open
    assert client.get("/api/directory").status_code == 200
    # grading succeeds with the ordinary fake settlement
    started = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert started.status_code == 202
    completed = wait_for_grade(client, started.json()["job_id"])
    assert completed["status"] == "completed"
    assert completed["points_charged"] == 3.0
    assert len(grading.page_adapter.write_calls) == 1
    # organize succeeds and adds exactly one row
    before_ids = {row["id"] for row in client.get("/api/exercises").json()["exercises"]}
    started = client.post("/api/exercises/organize", json={
        "course_id": COURSE_NET, "lecture_ids": [NET_L1],
    })
    organized = wait_for_organize(client, started.json()["job_id"])
    assert organized["status"] == "completed"
    after_ids = {row["id"] for row in client.get("/api/exercises").json()["exercises"]}
    assert after_ids - before_ids == {organized["exercise_id"]}
    assert platform.llm_calls[0]["operation"] == "grade"
    assert platform.llm_calls[1]["operation"] == "quiz"
