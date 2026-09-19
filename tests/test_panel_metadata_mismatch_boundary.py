from __future__ import annotations

from types import SimpleNamespace

from pku_sync.notion_meta.entities import ExerciseEntity
from pku_sync.panel.exercises import exercise_row, mismatch_notice_for


def _entity(*, marker: bool):
    return SimpleNamespace(
        id="exercise-boundary",
        url="https://www.notion.so/exercise-boundary",
        title="???????",
        course="?????",
        marker_present=marker,
        answer_present=True,
    )


def test_notion_exercise_entity_keeps_original_allowlist():
    entity = ExerciseEntity(
        id="exercise-boundary",
        url="https://www.notion.so/exercise-boundary",
        title="???????",
        course="?????",
        parent="course-boundary",
    )
    assert set(entity.model_dump()) == {
        "id", "url", "title", "course", "parent", "updated",
        "marker_present", "answer_present",
    }


def test_boundary_warns_only_for_explicit_marker_record_disagreement():
    assert mismatch_notice_for(_entity(marker=True), {"status": "graded"}) is False
    assert mismatch_notice_for(_entity(marker=False), {"status": "pending-grade"}) is False
    assert mismatch_notice_for(_entity(marker=True), {"status": "pending-grade"}) is True
    assert mismatch_notice_for(_entity(marker=False), {"status": "graded"}) is True
    assert mismatch_notice_for(_entity(marker=True), None) is False


def test_boundary_warning_does_not_override_marker_authoritative_status():
    row = exercise_row(_entity(marker=True), local_record={"status": "pending-grade"})
    assert row["status"] == "graded"
    assert row["mismatch_notice"] is True
    assert "mismatch_notice" not in exercise_row(_entity(marker=True))
