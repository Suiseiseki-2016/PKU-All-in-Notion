"""Write-back semantics for the metered exercise grader."""
from __future__ import annotations

import json

from pku_sync.panel.exercise_grader import _parse_grade_results


def test_mixed_grade_parser_covers_all_types_and_partial_credit():
    content = json.dumps({"questions": [
        {"number": 1, "type": "选择", "score": 2, "max_score": 2},
        {"number": 2, "type": "判断", "score": 0, "max_score": 2},
        {"number": 3, "type": "填空", "score": 1, "max_score": 2},
        {"number": 4, "type": "简答", "score": 2, "max_score": 2},
        {"number": 5, "type": "论述", "score": 0, "max_score": 2},
    ]}, ensure_ascii=False)
    parsed = _parse_grade_results(content)
    assert [item["type"] for item in parsed] == ["选择", "判断", "填空", "简答", "论述"]
    assert parsed[2]["partial"] is True
    assert parsed[2]["register_wrong"] is False
    assert [item["number"] for item in parsed if item["register_wrong"]] == [2, 5]
from dataclasses import dataclass
from pathlib import Path

from pku_sync.panel.exercise_grader import (
    ExerciseGrader,
    FakeGradingPageAdapter,
    _parse_grade_results,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import EXERCISE_GENERATED


@dataclass
class _WritebackRelay:
    balance: float = 10

    def __post_init__(self):
        self.grade_calls = 0
        self.quota_calls = 0

    def quota(self):
        self.quota_calls += 1
        return {"available": True, "llm_points_remaining": self.balance}

    def grade(self, prompt):
        self.grade_calls += 1
        self.balance -= 2
        return {
            "content": '{"score": 72, "questions": ['
            '{"number": 1, "type": "选择", "score": 2, "max_score": 2, "wrong_answer_key": "q1"},'
            '{"number": 2, "type": "判断", "score": 0, "max_score": 2, "wrong_answer_key": "q2"},'
            '{"number": 3, "type": "填空", "score": 1, "max_score": 2, "wrong_answer_key": "q3"},'
            '{"number": 4, "type": "简答", "score": 2, "max_score": 2, "wrong_answer_key": "q4"},'
            '{"number": 5, "type": "论述", "score": 0, "max_score": 2, "wrong_answer_key": "q5"}]}' ,
            "points_charged": 2,
        }


def test_marker_rerun_is_free_reuses_url_and_dedups_wrong_answers():
    relay = _WritebackRelay()
    adapter = FakeGradingPageAdapter()
    directory = build_fake_directory()
    directory.load()
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter, clock=lambda: "2025-01-01T12:00:00+08:00")
    first = grader.grade(grader.prepare(EXERCISE_GENERATED))
    first_calls = list(adapter.wrong_answer_calls)
    second = grader.grade(grader.prepare(EXERCISE_GENERATED))

    assert first["result_page_url"] == second["result_page_url"]
    assert second["points_charged"] == 0
    assert relay.grade_calls == 1
    assert relay.quota_calls == 1
    assert len(adapter.write_calls) == 1
    assert first_calls == [("dedup", "q2"), ("create", "q2"), ("dedup", "q5"), ("create", "q5")]
    assert adapter.wrong_answer_calls == first_calls


def test_answer_writeback_never_uses_replace_page_content():
    source = Path(__file__).parents[1].joinpath("pku_sync", "panel", "exercise_grader.py").read_text(encoding="utf-8-sig")
    assert "replace_page_content" not in source
    assert "update_result" in source


def test_parser_keeps_partial_credit_for_all_subjective_types():
    parsed = _parse_grade_results({"questions": [
        {"number": 3, "type": "填空", "score": 1, "max_score": 2},
        {"number": 4, "type": "简答", "score": 3, "max_score": 5},
        {"number": 5, "type": "论述", "score": 4, "max_score": 8},
    ]})
    assert [row["partial"] for row in parsed] == [True, True, True]
    assert not any(row["register_wrong"] for row in parsed)
