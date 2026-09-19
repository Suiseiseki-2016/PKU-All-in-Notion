from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pku_sync.panel.exercises import (
    EXERCISE_GRADED,
    EXERCISE_ORGANIZED,
    EXERCISE_PENDING_ANSWER,
    EXERCISE_PENDING_GRADE,
    EXERCISE_STATES,
    derive_exercise_state,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import PANEL_FORBIDDEN_STRINGS, build_panel_workspace
from pku_sync.panel.webapi import create_app


def test_state_derivation_is_exactly_the_four_approved_states():
    cases = [
        (False, False, False, EXERCISE_ORGANIZED),
        (False, False, True, EXERCISE_PENDING_ANSWER),
        (False, True, False, EXERCISE_PENDING_GRADE),
        (False, True, True, EXERCISE_PENDING_GRADE),
        (True, False, False, EXERCISE_GRADED),
        (True, True, True, EXERCISE_GRADED),
    ]
    for marker, answer, launched, expected in cases:
        assert derive_exercise_state(marker_present=marker, answer_present=answer, launch_started=launched) == expected
    assert set(EXERCISE_STATES) == {EXERCISE_ORGANIZED, EXERCISE_PENDING_ANSWER, EXERCISE_PENDING_GRADE, EXERCISE_GRADED}


def test_directory_mixes_existing_and_generated_exercises_with_one_schema():
    service = build_fake_directory(workspace=build_panel_workspace())
    client = TestClient(create_app(directory_service=service))
    response = client.get('/api/directory')
    assert response.status_code == 200
    rows = response.json()['exercises']
    assert len(rows) == 2
    assert all(set(row) == {'id', 'url', 'title', 'course', 'scope', 'status', 'status_label', 'score'} for row in rows)
    assert {row['status'] for row in rows} <= set(EXERCISE_STATES)


def test_exercise_directory_payload_is_metadata_only():
    service = build_fake_directory(workspace=build_panel_workspace())
    client = TestClient(create_app(directory_service=service))
    dumped = json.dumps(client.get('/api/directory').json(), ensure_ascii=False)
    for needle in (*PANEL_FORBIDDEN_STRINGS, 'HONEYPOT题目', 'HONEYPOT学生答案', 'HONEYPOT解析'):
        assert needle not in dumped


def test_exercise_read_failure_does_not_present_stale_rows():
    service = build_fake_directory(workspace=build_panel_workspace())
    client = TestClient(create_app(directory_service=service))
    assert client.get('/api/directory').status_code == 200
    service.provider._directory._client._ws.search_results.clear()
    failed = client.get('/api/directory')
    assert failed.status_code == 503
    assert 'exercises' not in failed.json()


def test_disconnected_directory_api_has_no_rows():
    service = build_fake_directory(workspace=build_panel_workspace())
    client = TestClient(create_app(directory_service=service))
    assert client.get('/api/directory').status_code == 200
    assert client.get('/api/exercises').status_code == 200
