"""Additive regression coverage for grading parent and settlement boundaries."""
from __future__ import annotations

import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from pku_sync.panel.exercise_grader import ExerciseGrader, GradeBlocked, GradeSettlementError, GradeTarget, GradingInput
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.grading_api import GradeJobController
from pku_sync.panel.jobs import ExerciseJobGate


@dataclass
class SettlementRelay:
    content: str
    balance: float = 20.0
    charge: float = 3.0

    def __post_init__(self):
        self.calls = 0

    def quota(self):
        return {"active": True, "available": True, "llm_points_remaining": self.balance}

    def grade(self, _prompt):
        self.calls += 1
        self.balance -= self.charge
        return {"content": self.content, "points_charged": self.charge}


class RecordingAdapter:
    def __init__(self, *, parent_error: Exception | None = None, write_error: Exception | None = None):
        self.parent_error = parent_error
        self.write_error = write_error
        self.read_calls = 0
        self.write_calls = 0

    def verify_parent(self, _target):
        if self.parent_error:
            raise self.parent_error

    def read_for_grading(self, _target):
        self.read_calls += 1
        return GradingInput(prompt="grade", unanswered=[])

    def write_result(self, *_args, **_kwargs):
        self.write_calls += 1
        if self.write_error:
            raise self.write_error


def _target() -> GradeTarget:
    return GradeTarget("grade", "exercise-page", "https://www.notion.so/exercise-page", "考前练习", "course-page", "计算机网络", "考前练习")


def test_operation_time_parent_failure_blocks_before_relay_call():
    relay = SettlementRelay('{"score": 88}')
    adapter = RecordingAdapter(parent_error=GradeBlocked("练习页已移出当前课程，无法批改，请刷新目录后重试。"))
    class Directory:
        def exercise_grade_target(self, _exercise_id):
            target = _target()
            return {"page_id": target.page_id, "page_url": target.page_url, "title": target.title,
                    "course_id": target.course_id, "course_title": target.course_title, "scope": target.scope}
    grader = ExerciseGrader(directory_service=Directory(), relay=relay, page_adapter=adapter)

    with pytest.raises(GradeBlocked, match="已移出当前课程"):
        grader.prepare("exercise-generated")

    assert adapter.read_calls == 0
    assert relay.calls == 0


@pytest.mark.parametrize("content,write_error", [("not-json", None), ('{"score": 88}', RuntimeError("notion write failed"))])
def test_post_charge_failure_exposes_settlement_without_success_or_partial_write(content, write_error):
    relay = SettlementRelay(content)
    adapter = RecordingAdapter(write_error=write_error)
    grader = ExerciseGrader(directory_service=build_fake_directory(), relay=relay, page_adapter=adapter)

    prepared = (_target(), GradingInput(prompt="grade", unanswered=[]))
    with pytest.raises(GradeSettlementError) as raised:
        grader.grade(prepared)

    error = raised.value
    assert error.points_charged == 3.0
    assert error.points_remaining == 17.0
    assert adapter.write_calls == (1 if write_error else 0)


class PreflightDirectory:
    def __init__(self):
        self.calls = 0

    def resolve_exercise_launch(self, _exercise_id):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("preflight failed")
        return SimpleNamespace(status="opened", fallback=None)


class ControllerService:
    def __init__(self):
        self.directory_service = PreflightDirectory()
        self.target = _target()

    def prepare(self, _exercise_id):
        return self.target, GradingInput(prompt="grade", unanswered=[])

    def grade(self, _prepared, *, force=False):
        return {"status": "completed", "exercise_id": self.target.page_id, "title": self.target.title, "score": 88,
                "graded_at": "2026-01-01T00:00:00+08:00", "points_charged": 0, "points_remaining": 20,
                "result_page_url": self.target.page_url}


def test_preflight_exception_releases_gate_for_subsequent_job():
    service = ControllerService()
    controller = GradeJobController(service, gate=ExerciseJobGate())

    with pytest.raises(RuntimeError, match="preflight failed"):
        controller.start("exercise-generated")

    body, code = controller.start("exercise-generated")
    assert code == 202
    assert body["status"] == "running"
    for _ in range(100):
        result, _ = controller.snapshot(body["job_id"])
        if result.get("status") != "running":
            break
        time.sleep(0.005)
    assert result["status"] == "completed"
