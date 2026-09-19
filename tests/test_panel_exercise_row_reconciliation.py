"""Additive coverage for exercise row reconciliation and disconnected actions."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pku_sync.panel.connection import build_fake_connection_service
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import EXERCISE_EXISTING, EXERCISE_GENERATED
from pku_sync.panel.webapi import create_app


APP_JS = Path(__file__).parents[1] / "pku_sync" / "panel" / "static" / "app.js"


def _client(*, connected: bool = True):
    directory = build_fake_directory()
    connection = build_fake_connection_service(
        connected=connected, directory_service=directory
    )
    return TestClient(
        create_app(directory_service=directory, connection_service=connection)
    ), directory, connection


def _row(client: TestClient, exercise_id: str) -> dict:
    response = client.get("/api/directory")
    assert response.status_code == 200
    return next(row for row in response.json()["exercises"] if row["id"] == exercise_id)


def test_successful_answer_launch_reconciles_the_row_to_pending_answer():
    client, _directory, _connection = _client()
    assert _row(client, EXERCISE_GENERATED)["status"] == "organized"

    launched = client.post(
        f"/api/exercises/{EXERCISE_GENERATED}/launch", json={"purpose": "answer"}
    )
    assert launched.status_code == 200
    # A fresh directory read is the server-side reconciliation surface. The
    # browser must perform this read before leaving for the stored URL.
    assert _row(client, EXERCISE_GENERATED)["status"] == "pending-answer"


def test_result_launch_keeps_a_graded_row_graded():
    client, _directory, _connection = _client()
    before = _row(client, EXERCISE_EXISTING)
    assert before["status"] == "graded"

    launched = client.post(
        f"/api/exercises/{EXERCISE_EXISTING}/launch", json={"purpose": "result"}
    )
    assert launched.status_code == 200
    assert _row(client, EXERCISE_EXISTING) == before


def test_disconnected_connection_does_not_expose_an_active_regrade_action():
    script = APP_JS.read_text(encoding="utf-8")
    row_start = script.index('var regrade = row.status === "graded"')
    row_end = script.index("var mismatch =", row_start)
    regrade_source = script[row_start:row_end]
    assert "regradeDisabled" in regrade_source

    handler_start = script.index("function confirmRegrade(")
    handler_end = script.index("\n  function startGrading", handler_start)
    handler_source = script[handler_start:handler_end]
    assert "!state.connection.connected" in handler_source


def test_failed_answer_launch_does_not_record_or_change_the_row():
    directory = build_fake_directory("exercise-fault-launch")
    connection = build_fake_connection_service(
        connected=True, directory_service=directory
    )
    client = TestClient(
        create_app(directory_service=directory, connection_service=connection)
    )
    before = _row(client, EXERCISE_GENERATED)
    failed = client.post(
        f"/api/exercises/{EXERCISE_GENERATED}/launch", json={"purpose": "answer"}
    )
    assert failed.status_code == 502
    assert _row(client, EXERCISE_GENERATED) == before
    assert directory.exercise_events.launched_ids() == set()
