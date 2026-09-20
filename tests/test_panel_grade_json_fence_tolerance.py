"""Additive fence-tolerance coverage for the grader's pku-e2e-grade-v1 gate.

One stray Markdown code fence around an otherwise contract-valid grade
response is unwrapped and accepted; a second fence, a nested fence, malformed
JSON inside the wrapper, or any contract-invalid content is rejected exactly
as today, after the charge but before any Notion write or wrong-answer
registration. A durable resume from persisted raw fenced content completes
without a second relay call. All relays are deterministic fakes — no real
LLM, Notion, or other spend.
"""
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
    GradingInput,
)
from pku_sync.panel.exercises import JsonGradingRecordStore
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import EXERCISE_GENERATED

GRADED_AT = "2026-09-20T12:00:00+08:00"


def valid_result() -> dict:
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


def result_json() -> str:
    return json.dumps(valid_result(), ensure_ascii=False)


def fenced(payload: str) -> str:
    return f"```json\n{payload}\n```"


class Relay:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0
        self.balance = 20.0

    def quota(self) -> dict:
        return {"available": True, "llm_points_remaining": self.balance}

    def grade(self, _prompt: str) -> dict:
        self.calls += 1
        self.balance -= 2.0
        return {"content": self.content, "points_charged": 2.0}


class ContractAdapter(FakeGradingPageAdapter):
    def read_for_grading(self, target):
        return GradingInput(
            prompt="strict fixture prompt",
            unanswered=[],
            answer_fingerprint="fixture-answers-v1",
            answer_provenance="known",
            result_contract=GRADE_RESULT_CONTRACT_VERSION,
            result_marker="E2E_GRADE_RESULT",
        )


class DirectoryStub:
    def __init__(self, store: JsonGradingRecordStore):
        self.grading_record_store = store
        self.local_grading_records = store.snapshot()

    def exercise_grade_target(self, exercise_id):
        return {
            "page_id": exercise_id,
            "page_url": f"https://www.notion.so/{exercise_id}",
            "title": "[E2E] 五题批改契约",
            "course_id": "course-page",
            "course_title": "计算机网络",
            "scope": "考前练习",
        }

    def record_grading(self, exercise_id, record):
        self.grading_record_store.put(exercise_id, record)
        self.local_grading_records[exercise_id] = copy.deepcopy(record)


def make_grader(content: str):
    directory = build_fake_directory()
    directory.load()
    relay = Relay(content)
    adapter = ContractAdapter()
    grader = ExerciseGrader(
        directory_service=directory,
        relay=relay,
        page_adapter=adapter,
        clock=lambda: GRADED_AT,
    )
    return grader, relay, adapter


def _assert_accepted(summary, relay, adapter):
    assert summary["status"] == "completed" and summary["score"] == 50
    assert summary["points_charged"] == 2.0 and summary["points_remaining"] == 18.0
    assert relay.calls == 1
    assert len(adapter.write_calls) == 1
    written = json.loads(adapter.write_calls[0]["content"])
    assert written["contract_version"] == GRADE_RESULT_CONTRACT_VERSION
    assert [row["question_id"] for row in written["questions"]] == [
        "Q1", "Q2", "Q3", "Q4", "Q5",
    ]
    assert adapter.wrong_answer_calls == [
        ("dedup", "Q2"), ("create", "Q2"),
        ("dedup", "Q5"), ("create", "Q5"),
    ]


@pytest.mark.parametrize(
    "wrapper",
    ["```json\n{p}\n```", "```\n{p}\n```", "```JSON\n{p}\n```", "~~~json\n{p}\n~~~"],
)
def test_single_fence_around_contract_valid_grade_is_unwrapped_and_accepted(wrapper):
    grader, relay, adapter = make_grader(wrapper.replace("{p}", result_json()))

    summary = grader.grade(grader.prepare(EXERCISE_GENERATED))

    _assert_accepted(summary, relay, adapter)


def test_fenced_contract_version_is_detected_without_adapter_contract_flag():
    grader, relay, adapter = make_grader(fenced(result_json()))
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

    summary = grader.grade(prepared)

    _assert_accepted(summary, relay, adapter)


def test_second_fence_around_contract_result_is_rejected_after_charge_without_write():
    payload = result_json()
    grader, relay, adapter = make_grader(f"{fenced(payload)}\n{fenced(payload)}")

    with pytest.raises(
        GradeSettlementError, match="AI 返回的批改结果不完整，请重试。"
    ) as raised:
        grader.grade(grader.prepare(EXERCISE_GENERATED))

    assert raised.value.points_charged == 2.0 and relay.calls == 1
    assert adapter.write_calls == [] and adapter.wrong_answer_calls == []


def test_nested_fence_around_contract_result_is_rejected_after_charge_without_write():
    payload = result_json()
    grader, relay, adapter = make_grader(f"```json\n{fenced(payload)}\n```")

    with pytest.raises(
        GradeSettlementError, match="AI 返回的批改结果不完整，请重试。"
    ) as raised:
        grader.grade(grader.prepare(EXERCISE_GENERATED))

    assert raised.value.points_charged == 2.0 and relay.calls == 1
    assert adapter.write_calls == [] and adapter.wrong_answer_calls == []


def test_malformed_json_inside_fence_is_rejected_after_charge_without_write():
    grader, relay, adapter = make_grader(
        '```json\n{"contract_version": "pku-e2e-grade-v1", "questions": [\n```'
    )

    with pytest.raises(
        GradeSettlementError, match="AI 返回的批改结果不完整，请重试。"
    ) as raised:
        grader.grade(grader.prepare(EXERCISE_GENERATED))

    assert raised.value.points_charged == 2.0 and relay.calls == 1
    assert adapter.write_calls == [] and adapter.wrong_answer_calls == []


def _mutations():
    return {
        "missing": lambda value: value["questions"].pop(),
        "duplicate": lambda value: value["questions"][4].update(question_id="Q2"),
        "out-of-range": lambda value: value["questions"][2].update(score=3),
        "wrong-type": lambda value: value["questions"][3].update(type="论述"),
        "unstable-identity": lambda value: value["questions"][1].update(wrong_answer_key="row-two"),
        "wrong-id-type": lambda value: value["questions"][1].update(question_id=[]),
        "inconsistent-total": lambda value: value.update(score=88),
    }


@pytest.mark.parametrize("name", list(_mutations()))
def test_contract_invalid_inside_fence_is_rejected_after_charge_without_write(name):
    result = copy.deepcopy(valid_result())
    _mutations()[name](result)
    grader, relay, adapter = make_grader(fenced(json.dumps(result, ensure_ascii=False)))

    with pytest.raises(GradeSettlementError, match="AI 返回的批改结果不完整") as raised:
        grader.grade(grader.prepare(EXERCISE_GENERATED))

    assert raised.value.points_charged == 2.0 and relay.calls == 1
    assert adapter.write_calls == [] and adapter.wrong_answer_calls == []


def test_durable_resume_from_persisted_fenced_content_needs_no_second_relay_call(tmp_path):
    store = JsonGradingRecordStore(tmp_path / "grading_records.json")
    charged = {
        "version": 2, "status": "pending", "phase": "relay_succeeded",
        "operation_id": "fence-op-1", "operation": "grade",
        "page_id": "exercise-page", "page_url": "https://www.notion.so/exercise-page",
        "course_id": "course-page", "course_title": "计算机网络",
        "title": "[E2E] 五题批改契约", "scope": "考前练习",
        "answer_fingerprint": "fixture-answers-v1", "answer_provenance": "known",
        "regrade": False, "result_marker": "E2E_GRADE_RESULT",
        "result_contract": GRADE_RESULT_CONTRACT_VERSION,
        "content": fenced(result_json()), "points_charged": 2.0,
        "points_remaining": 18.0,
        "result_page_url": "https://www.notion.so/exercise-page",
        "graded_at": GRADED_AT,
    }
    store.put("exercise-page", charged)
    directory = DirectoryStub(store)
    relay = Relay(fenced(result_json()))
    adapter = FakeGradingPageAdapter()
    grader = ExerciseGrader(
        directory_service=directory,
        relay=relay,
        page_adapter=adapter,
        clock=lambda: GRADED_AT,
    )

    summary = grader.grade(grader.prepare("exercise-page"))

    # The charged operation completes from durable state: no second charge.
    assert relay.calls == 0
    assert summary["status"] == "completed" and summary["score"] == 50
    assert summary["points_charged"] == 2.0 and summary["points_remaining"] == 18.0
    written = json.loads(adapter.write_calls[0]["content"])
    assert written["contract_version"] == GRADE_RESULT_CONTRACT_VERSION
    assert adapter.wrong_answer_calls == [
        ("dedup", "Q2"), ("create", "Q2"),
        ("dedup", "Q5"), ("create", "Q5"),
    ]
    # The completed durable record keeps settlement and metadata only
    # ("content" is transient by design), proving durable convergence.
    record = store.snapshot()["exercise-page"]
    assert record["status"] == "graded" and record["phase"] == "completed"
    assert record["points_charged"] == 2.0 and "content" not in record
