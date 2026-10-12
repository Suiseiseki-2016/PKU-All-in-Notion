"""Identity-only launch, retry, missing-page, and honest sync contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pku_sync.panel.directory import (
    STATE_EMPTY,
    STATE_ERROR,
    SYNC_ERROR_UNAVAILABLE,
    DirectoryService,
)
from pku_sync.panel.fake_directory import PanelDirectoryProvider, build_fake_directory
from pku_sync.panel.fake_workspace import (
    COURSE_CS,
    COURSE_NET,
    EXERCISE_EXISTING,
    EXERCISE_GENERATED,
    NET_L1,
    NET_L2,
    build_panel_workspace,
)
from pku_sync.panel.student_ui import student_page
from pku_sync.panel.webapi import create_app


def make_client(variant: str = "normal"):
    service = build_fake_directory(variant=variant)
    return TestClient(create_app(directory_service=service)), service


def directory(client: TestClient) -> dict:
    response = client.get("/api/directory")
    assert response.status_code == 200
    return response.json()


def exercise(client: TestClient, exercise_id: str = EXERCISE_GENERATED) -> dict:
    return next(row for row in directory(client)["exercises"] if row["id"] == exercise_id)


@pytest.mark.parametrize("variant", ["fault-launch", "transport-launch"])
def test_launch_failure_has_no_success_url_and_transport_retry_keeps_identity(variant):
    client, service = make_client(variant)
    directory(client)
    before = list(service.provider.client.calls)
    failed = client.post("/api/launch", json={"target_id": NET_L1})
    assert failed.status_code == 502
    assert failed.json() == {"detail": "打开没有成功，请稍后重试。"}
    assert service.provider.client.calls == before
    if variant == "transport-launch":
        retried = client.post("/api/launch", json={"target_id": NET_L1})
        assert retried.status_code == 200
        assert retried.json()["status"] == "opened"
        assert retried.json()["target_id"] == NET_L1
        assert retried.json()["url"] == f"https://www.notion.so/{NET_L1.replace('-', '')}"


@pytest.mark.parametrize(
    "variant,target_id,course_id",
    [("normal", "missing-lecture", COURSE_CS), ("missing-lecture", NET_L2, COURSE_NET)],
)
def test_missing_page_never_searches_or_fabricates_url_and_keeps_course_fallback(
    variant, target_id, course_id
):
    client, service = make_client(variant)
    data = directory(client)
    if variant == "missing-lecture":
        course = next(item for item in data["courses"] if item["id"] == course_id)
        lecture = next(item for item in course["lectures"] if item["id"] == target_id)
        assert lecture["page_state"] == "missing"
        assert lecture["url"] == ""
    before = list(service.provider.client.calls)
    response = client.post(
        "/api/launch",
        json={"target_id": target_id, "target_type": "lecture", "course_id": course_id},
    )
    body = response.json()
    assert body["status"] == "missing_mapping"
    assert body["url"] is None
    assert body["fallback"]["id"] == course_id
    assert service.provider.client.calls == before
    if variant == "missing-lecture":
        assert response.status_code == 404
        assert body == {
            "detail": "该条目还没有对应的 Notion 页面，可以先用课程页打开。",
            "status": "missing_mapping",
            "target_id": NET_L2,
            "url": None,
            "fallback": {
                "id": COURSE_NET,
                "url": f"https://www.notion.so/{COURSE_NET.replace('-', '')}",
                "title": "计算机网络",
            },
        }


def test_missing_course_fallback_requires_a_stored_page_url():
    class MissingCourseUrlProvider(PanelDirectoryProvider):
        def load(self):
            data = super().load()
            for item in [*data.courses, *data.lectures]:
                if item.id in {COURSE_NET, NET_L2}:
                    item.url = ""
            return data

    service = DirectoryService(MissingCourseUrlProvider(build_panel_workspace()))
    service.load()
    result = service.resolve_launch(NET_L2, course_id=COURSE_NET)
    assert result.url is None
    assert result.fallback is None


@pytest.mark.parametrize("variant", ["empty", "usage-cap"])
def test_sync_distinguishes_verified_empty_from_unavailable(variant):
    client, _ = make_client(variant)
    response = client.get("/api/directory")
    state = client.get("/api/sync/state").json()
    if variant == "empty":
        assert response.status_code == 200
        assert response.json()["courses"] == []
        assert response.json()["sync"]["state"] == STATE_EMPTY
        assert state["state"] == STATE_EMPTY
    else:
        assert response.status_code == 503
        assert response.json() == {"detail": SYNC_ERROR_UNAVAILABLE}
        assert state == {
            "state": STATE_ERROR,
            "last_sync_at": None,
            "error_reason": SYNC_ERROR_UNAVAILABLE,
        }


@pytest.mark.parametrize(
    "purpose,target_id", [("answer", EXERCISE_GENERATED), ("result", EXERCISE_EXISTING)]
)
def test_exercise_launch_uses_stored_identity_and_only_answer_marks_started(purpose, target_id):
    client, service = make_client()
    row = exercise(client, target_id)
    if purpose == "result":
        assert row["status"] == "graded"
    service.provider.client.calls.clear()
    response = client.post(f"/api/exercises/{target_id}/launch", json={"purpose": purpose})
    assert response.status_code == 200
    assert response.json() == {
        "status": "opened", "target_id": target_id, "url": row["url"], "fallback": None
    }
    assert service.provider.client.calls == []
    assert service.exercise_events.launched_ids() == ({target_id} if purpose == "answer" else set())


@pytest.mark.parametrize("action", ["launch", "grade"])
def test_transient_exercise_failure_preserves_records_and_launch_is_retryable(action):
    client, service = make_client("exercise-fault-launch")
    before = exercise(client)
    service.provider.client.calls.clear()
    endpoint = f"/api/exercises/{EXERCISE_GENERATED}/{action}"
    failed = (
        client.post(endpoint, json={"purpose": "answer"})
        if action == "launch" else client.post(endpoint)
    )
    assert failed.status_code == 502
    expected = (
        {"detail": "打开没有成功，请稍后重试。"} if action == "launch"
        else {
            "status": "failed", "reason": "打开没有成功，请稍后重试。", "fallback": None
        }
    )
    assert failed.json() == expected
    assert service.exercise_events.launched_ids() == set()
    assert service.provider.client.calls == []
    if action == "launch":
        assert exercise(client) == before
        retried = client.post(endpoint, json={"purpose": "answer"})
        assert retried.status_code == 200
        assert retried.json()["url"] == before["url"]
        assert service.exercise_events.launched_ids() == {EXERCISE_GENERATED}


def test_missing_exercise_identity_blocks_launch_and_grade_with_course_fallback():
    client, service = make_client("exercise-missing")
    assert exercise(client)["url"] == ""
    service.provider.client.calls.clear()
    launch = client.post(f"/api/exercises/{EXERCISE_GENERATED}/launch", json={"purpose": "answer"})
    grade = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    assert launch.status_code == 404
    assert launch.json()["status"] == "page_identity_missing"
    assert launch.json()["detail"] == (
        "该练习还没有对应的 Notion 页面，暂时无法作答或批改。可以先打开课程页查看。"
    )
    assert launch.json()["url"] is None
    assert launch.json()["fallback"]["id"] == COURSE_NET
    assert grade.status_code == 409
    assert grade.json()["status"] == "page_identity_missing"
    assert grade.json()["reason"]
    assert grade.json()["fallback"]["id"] == COURSE_NET
    assert service.exercise_events.launched_ids() == set()
    assert service.provider.client.calls == []


def test_ui_launch_and_sync_contracts():
    # Source-level guards, not browser E2E. Read each asset once rather than
    # repeating the same copy and plumbing assertions in six feature files.
    page = student_page()
    script_path = Path(__file__).parents[1] / "pku_sync/panel/static/app.js"
    script = script_path.read_text(encoding="utf-8")
    for copy in (
        "待建讲次页", "讲次页尚未建立 · 整理完成后自动出现", "查看课程页",
        "正在同步索引", "同步遇到问题", "还没有已索引的课程", "同步完成",
        "重新同步", "回到总览", "重新打开", "打开没有成功",
    ):
        assert copy in page
    for key, value in (
        ("missing_title", "待建讲次页"),
        ("missing_body", "讲次页尚未建立 · 整理完成后自动出现"),
        ("fallback", "查看课程页"),
    ):
        assert f'"{key}": "{value}"' in page
    assert "window.open(" not in page
    for state in ("loading", "error", "empty", "success"):
        assert f'data-state="{state}"' in script
    for fragment in (
        'page_state === "missing"', 'disabled: lecture.page_state === "missing"',
        ".catch(function ()", 'status: "failed"', '"answer"', '"result"',
        "page_identity_missing", "retry-exercise-launch", "launch-exercise-fallback",
        "result.body.fallback.url", "openNotionPreview(result.body.url",
        "answerLaunches: {}", "preserveOnError", "applyAnswerLaunches",
    ):
        assert fragment in script
    assert "course.title === row.course" not in script
    assert "window.location.assign(result.body.url" not in script

    def body(start: str, end: str) -> str:
        first = script.index(f"function {start}(")
        return script[first:script.index(f"\n  function {end}", first)]

    for start, end in (("selectLecture", "setMaterialView"), ("openCourse", "selectLecture")):
        assert "state.launch = null;" in body(start, end)
    disabled = body("lectureLaunchDisabled", "launchPanel")
    assert 'current.status === "missing"' in disabled
    assert "current.targetId === lecture.id" in disabled
    for start, end in (("launchPanel", "launchFeedback"), ("lectureScreen", "render")):
        assert "disabled: lectureLaunchDisabled(lecture)" in body(start, end)
    answer = body("launchExercise", "launch(kind")
    assert "state.answerLaunches[targetId] = true;" in answer
    assert 'loadDirectory({ preserveOnError: true })' in answer
    assert "reconcileExerciseRow(targetId)" in answer
