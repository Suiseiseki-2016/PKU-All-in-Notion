"""Additive coverage for launch and answer-row recovery boundaries."""

from __future__ import annotations

from pathlib import Path


APP_JS = Path(__file__).parents[1] / "pku_sync" / "panel" / "static" / "app.js"


def test_current_lecture_controls_disable_after_missing_mapping_response():
    script = APP_JS.read_text(encoding="utf-8")

    helper_start = script.index("function lectureLaunchDisabled(")
    helper_end = script.index("\n  function launchPanel", helper_start)
    helper = script[helper_start:helper_end]
    assert 'current.status === "missing"' in helper
    assert 'current.targetId === lecture.id' in helper

    launch_start = script.index("function launchPanel(")
    launch_end = script.index("\n  function launchFeedback", launch_start)
    launch_body = script[launch_start:launch_end]
    assert "disabled: lectureLaunchDisabled(lecture)" in launch_body

    screen_start = script.index("function lectureScreen(")
    screen_end = script.index("\n  function render", screen_start)
    screen_body = script[screen_start:screen_end]
    assert "disabled: lectureLaunchDisabled(lecture)" in screen_body


def test_answer_launch_preserves_local_transition_when_refresh_fails():
    script = APP_JS.read_text(encoding="utf-8")

    assert "answerLaunches: {}" in script
    assert "preserveOnError" in script
    assert "applyAnswerLaunches" in script
    launch_start = script.index("function launchExercise(")
    launch_end = script.index("\n  function launch(kind", launch_start)
    launch_body = script[launch_start:launch_end]
    assert "state.answerLaunches[targetId] = true;" in launch_body
    assert 'loadDirectory({ preserveOnError: true })' in launch_body
    assert "reconcileExerciseRow(targetId)" in launch_body
