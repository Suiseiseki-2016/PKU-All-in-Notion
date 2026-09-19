"""Regression coverage for charged grading recovery boundaries."""
from __future__ import annotations

import json
from dataclasses import dataclass

from pku_sync.panel.exercise_grader import ExerciseGrader, GradeTarget, GradingInput


@dataclass
class _Relay:
    balance: float = 20.0

    def __post_init__(self):
        self.calls = 0

    def quota(self):
        return {"active": True, "available": True, "llm_points_remaining": self.balance}

    def grade(self, _prompt):
        self.calls += 1
        self.balance -= 3
        return {"content": json.dumps({"score": 88}), "points_charged": 3}


class _Directory:
    def __init__(self, *, fail_record_once=False):
        self.fail_record_once = fail_record_once
        self.records = {}

    def record_grading(self, exercise_id, record):
        if self.fail_record_once:
            self.fail_record_once = False
            raise OSError("record store unavailable")
        self.records[exercise_id] = dict(record)


class _Adapter:
    def __init__(self):
        self.writes = []

    def write_result(self, target, *, content, score, graded_at):
        self.writes.append((target.page_id, content, score, graded_at))


def _target():
    return GradeTarget("grade", "exercise-page", "https://www.notion.so/exercise-page", "考前练习", "course-page", "计算机网络", "考前练习")


def _prepared():
    return _target(), GradingInput(prompt="grade", unanswered=[])


def test_record_store_failure_keeps_settlement_and_retry_reuses_one_charge():
    relay = _Relay()
    directory = _Directory(fail_record_once=True)
    adapter = _Adapter()
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)

    try:
        grader.grade(_prepared())
    except Exception as first:
        assert first.points_charged == 3
        assert first.points_remaining == 17
    else:
        raise AssertionError("record failure must be settlement-aware")
    assert len(adapter.writes) == 0

    result = grader.grade(_prepared())
    assert result["status"] == "completed"
    assert result["points_charged"] == 3
    assert relay.calls == 1
    assert len(adapter.writes) == 1




class _FailingAdapter(_Adapter):
    def __init__(self):
        super().__init__()
        self.fail_once = True

    def write_result(self, target, *, content, score, graded_at):
        if self.fail_once:
            self.fail_once = False
            raise OSError("notion write unavailable")
        super().write_result(target, content=content, score=score, graded_at=graded_at)


def test_writeback_failure_retries_without_second_charge():
    relay = _Relay()
    directory = _Directory()
    adapter = _FailingAdapter()
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)

    try:
        grader.grade(_prepared())
    except Exception as first:
        assert first.points_charged == 3
        assert first.points_remaining == 17
    else:
        raise AssertionError("writeback failure must be settlement-aware")

    result = grader.grade(_prepared())
    assert result["status"] == "completed"
    assert result["points_charged"] == 3
    assert result["points_remaining"] == 17
    assert relay.calls == 1
    assert len(adapter.writes) == 1
