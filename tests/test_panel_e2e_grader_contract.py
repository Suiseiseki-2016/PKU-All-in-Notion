"""Pinned, spend-free coverage for the adopted five-question grade contract."""
from __future__ import annotations

import copy
import json

import pytest

from pku_sync.panel.exercise_grader import (
    E2E_GRADE_QUESTION_RUBRIC,
    GRADE_RESULT_CONTRACT_VERSION,
    ExerciseGrader,
    FakeGradingPageAdapter,
    GradeSettlementError,
    GradeTarget,
    GradingInput,
    _grading_prompt,
    _validate_grade_result,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import EXERCISE_GENERATED


def _valid_result():
    return {
        "contract_version": GRADE_RESULT_CONTRACT_VERSION,
        "score": 50,
        "earned_points": 5,
        "max_points": 10,
        "questions": [
            {
                **item.provider_schema(),
                "score": item.expected_score,
                "outcome": item.expected_outcome,
                "register_wrong": item.register_wrong,
                "wrong_answer_key": item.question_id,
                "feedback": f"{item.question_id} feedback",
            }
            for item in E2E_GRADE_QUESTION_RUBRIC
        ],
    }


class _Relay:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def quota(self):
        return {"available": True, "llm_points_remaining": 20.0}

    def grade(self, _prompt):
        self.calls += 1
        content = self.result if isinstance(self.result, str) else json.dumps(self.result, ensure_ascii=False)
        return {"content": content, "points_charged": 2.0}


class _WriteTrackingAdapter(FakeGradingPageAdapter):
    def read_for_grading(self, target):
        return GradingInput(
            prompt="strict fixture prompt",
            unanswered=[],
            answer_fingerprint="fixture-answers-v1",
            answer_provenance="known",
            result_contract=GRADE_RESULT_CONTRACT_VERSION,
            result_marker="E2E_GRADE_RESULT",
        )


def _grader(result):
    directory = build_fake_directory()
    directory.load()
    relay = _Relay(result)
    adapter = _WriteTrackingAdapter()
    grader = ExerciseGrader(
        directory_service=directory,
        relay=relay,
        page_adapter=adapter,
        clock=lambda: "2026-09-20T12:00:00+08:00",
    )
    return grader, relay, adapter


def _target(directory, title):
    data = directory.exercise_grade_target(EXERCISE_GENERATED)
    return GradeTarget(
        "grade",
        data["page_id"],
        data["page_url"],
        title,
        data["course_id"],
        data["course_title"],
        data["scope"],
    )


def test_provider_prompt_pins_exact_schema_types_identity_and_rubric():
    directory = build_fake_directory("grade-slow")
    directory.load()
    target = _target(directory, "[E2E] 五题批改契约")
    prompt, unanswered = _grading_prompt(
        target, directory.provider.client._ws.children[EXERCISE_GENERATED]
    )
    payload = json.loads(prompt)
    assert unanswered == []
    contract = payload["response_contract"]
    assert contract["version"] == GRADE_RESULT_CONTRACT_VERSION
    assert [row["question_id"] for row in contract["questions"]] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert [row["type"] for row in contract["questions"]] == ["选择", "判断", "填空", "简答", "论述"]
    assert [row["expected_score"] for row in contract["questions"]] == [2, 0, 1, 2, 0]
    assert [row["expected_outcome"] for row in contract["questions"]] == ["correct", "wrong", "partial", "correct", "wrong"]
    assert [row["question_id"] for row in contract["questions"] if row["register_wrong"]] == ["Q2", "Q5"]
    assert contract["score_formula"] == "earned_points / max_points * 100"
    assert contract["wrong_answer_question_ids"] == ["Q2", "Q5"]


def test_provider_prompt_keeps_fixture_contract_out_of_general_exercises():
    directory = build_fake_directory("grade-slow")
    directory.load()
    prompt, _ = _grading_prompt(
        _target(directory, "普通练习"),
        directory.provider.client._ws.children[EXERCISE_GENERATED],
    )
    assert "response_contract" not in json.loads(prompt)


def test_valid_result_preserves_identity_partial_credit_and_wrong_semantics():
    validated = _validate_grade_result(_valid_result())
    assert validated["score"] == 50
    assert [row["question_id"] for row in validated["questions"]] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert validated["questions"][2]["partial"] is True
    assert [row["question_id"] for row in validated["questions"] if row["register_wrong"]] == ["Q2", "Q5"]

    grader, relay, adapter = _grader(_valid_result())
    summary = grader.grade(grader.prepare(EXERCISE_GENERATED))
    assert summary["score"] == 50 and relay.calls == 1 and len(adapter.write_calls) == 1
    assert adapter.wrong_answer_calls == [
        ("dedup", "Q2"), ("create", "Q2"),
        ("dedup", "Q5"), ("create", "Q5"),
    ]


def _mutations():
    return {
        "missing": lambda value: value["questions"].pop(),
        "duplicate": lambda value: value["questions"][4].update(question_id="Q2"),
        "out-of-range": lambda value: value["questions"][2].update(score=3),
        "wrong-type": lambda value: value["questions"][3].update(type="论述"),
        "wrong-partial-semantics": lambda value: value["questions"][2].update(outcome="wrong"),
        "wrong-registration-semantics": lambda value: value["questions"][2].update(register_wrong=True),
        # wrong_answer_key/earned/score echoes are now normalized from the
        # pinned per-question result (feature m4-fix-grade-contract-mechanical-
        # clauses); their residual type/range boundaries are asserted here and
        # the verbatim-response replay stays additive in
        # tests/test_panel_grade_contract_mechanical_clauses.py.
        "non-string-key": lambda value: value["questions"][1].update(wrong_answer_key=[]),
        "wrong-id-type": lambda value: value["questions"][1].update(question_id=[]),
        "bogus-max": lambda value: value.update(max_points=12),
    }


@pytest.mark.parametrize("name", list(_mutations()))
def test_invalid_complete_result_fails_before_any_page_or_wrong_answer_write(name):
    result = copy.deepcopy(_valid_result())
    _mutations()[name](result)
    grader, relay, adapter = _grader(result)
    with pytest.raises(GradeSettlementError, match="AI 返回的批改结果不完整") as raised:
        grader.grade(grader.prepare(EXERCISE_GENERATED))
    assert raised.value.points_charged == 2.0 and relay.calls == 1
    assert adapter.write_calls == [] and adapter.wrong_answer_calls == []


def test_malformed_json_fails_before_any_page_or_wrong_answer_write():
    grader, relay, adapter = _grader("{not-json")
    with pytest.raises(GradeSettlementError, match="AI 返回的批改结果不完整"):
        grader.grade(grader.prepare(EXERCISE_GENERATED))
    assert relay.calls == 1
    assert adapter.write_calls == [] and adapter.wrong_answer_calls == []


def test_versioned_response_is_validated_even_without_adapter_contract_flag():
    grader, relay, adapter = _grader(_valid_result())
    target, grading_input = grader.prepare(EXERCISE_GENERATED)
    prepared = (
        target,
        GradingInput(
            prompt=grading_input.prompt,
            unanswered=[],
            answer_fingerprint="fixture-answers-v1",
            answer_provenance="known",
        ),
    )
    result = copy.deepcopy(_valid_result())
    result["questions"].pop()
    relay.result = result
    with pytest.raises(GradeSettlementError, match="AI 返回的批改结果不完整"):
        grader.grade(prepared)
    assert adapter.write_calls == [] and adapter.wrong_answer_calls == []