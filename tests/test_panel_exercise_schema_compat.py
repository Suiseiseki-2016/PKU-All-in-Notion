from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from pku_sync.panel.exercises import exercise_row
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import build_panel_workspace
from pku_sync.panel.webapi import create_app


LEGACY_EXERCISE_KEYS = {
    "id", "url", "title", "course", "scope", "status", "status_label", "score",
}


def test_ordinary_fake_directory_rows_keep_legacy_schema():
    service = build_fake_directory(workspace=build_panel_workspace())
    payload = TestClient(create_app(directory_service=service)).get("/api/directory").json()
    assert payload["exercises"]
    assert all(set(row) == LEGACY_EXERCISE_KEYS for row in payload["exercises"])


def test_true_mismatch_row_keeps_warning_flag_and_ui_alert_surface():
    entity = SimpleNamespace(
        id="exercise-mismatch", url="https://example.test/exercise-mismatch",
        title="?????", course="?????", marker_present=False,
        answer_present=True, mismatch_notice=True,
    )
    row = exercise_row(entity)
    assert row["mismatch_notice"] is True
    html = TestClient(create_app(directory_service=build_fake_directory(workspace=build_panel_workspace()))).get("/app").text
    app_js = TestClient(create_app(directory_service=build_fake_directory(workspace=build_panel_workspace()))).get("/app/app.js").text
    assert "mismatch_notice" in html
    assert 'role="alert"' in app_js
