"""Additive, spend-free coverage for the grade-contract mechanical clauses.

The Window D real grade leg returned a charged response whose per-question
rows match the pinned rubric exactly but whose two mechanical echo fields did
not: ``wrong_answer_key`` carried the wrong-answer TEXT (or null for correct
questions) instead of the per-question ``question_id``, and top-level
``earned_points=6`` did not equal its own per-question score sum (5). The
product's strict pku-e2e-grade-v1 validator rejected that charged response,
blocking the durable zero-spend resume.

This feature pins the approved protocol (VAL-EXER-035 Gap 2):
- the ``response_contract`` prompt states both clauses explicitly, and
- ``_validate_grade_result`` normalizes the mechanical echo fields from the
  pinned per-question result instead of rejecting the charged response.

VERBATIM_RESPONSE below is copied byte-for-byte from the charged relay
response the product durably preserved at
``<missionDir>/validation/m4-real-e2e/window-d-exercise-lifecycle/
evidence/44-durable-grade-record.json`` (record ``3e191b6f-...``, the
``content`` string), and the durable-resume test replays the exact retained
charge record fields (operation id, settlement, graded_at). Every relay is a
deterministic fake — no real LLM, Notion, or other spend.
"""
from __future__ import annotations

import copy
import json

import pytest

from pku_sync.panel.exercise_grader import (
    E2E_GRADE_QUESTION_RUBRIC,
    GRADE_RESULT_CONTRACT_VERSION,
    GRADE_RESULT_INVALID_REASON,
    ExerciseGrader,
    FakeGradingPageAdapter,
    GradeBlocked,
    GradeSettlementError,
    GradeTarget,
    GradingInput,
    _grading_prompt,
    _validate_grade_result,
)
from pku_sync.panel.exercises import JsonGradingRecordStore
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import EXERCISE_GENERATED

GRADED_AT = "2026-09-20T22:03:56+08:00"
SUMMED_EARNED = sum(item.expected_score for item in E2E_GRADE_QUESTION_RUBRIC)

# Verbatim charged relay response preserved in the Window D evidence pack.
# Value of 44-durable-grade-record.json -- record "3e191b6f-53e1-819d-be1e-
# fa826feed319" -- the "content" string, copied byte-for-byte.
VERBATIM_RESPONSE = """{
  "contract_version": "pku-e2e-grade-v1",
  "score": 60,
  "earned_points": 6,
  "max_points": 10,
  "questions": [
    {
      "question_id": "Q1",
      "number": 1,
      "type": "选择",
      "score": 2,
      "max_score": 2,
      "outcome": "correct",
      "register_wrong": false,
      "wrong_answer_key": null,
      "feedback": "回答正确。TCP/IP体系结构包含网络接口层、网际层、传输层和应用层。"
    },
    {
      "question_id": "Q2",
      "number": 2,
      "type": "判断",
      "score": 0,
      "max_score": 2,
      "outcome": "wrong",
      "register_wrong": true,
      "wrong_answer_key": "正确",
      "feedback": "回答错误。正确答案应为：正确。协议的核心要素确实包括语法、语义和同步（时序）。"
    },
    {
      "question_id": "Q3",
      "number": 3,
      "type": "填空",
      "score": 1,
      "max_score": 2,
      "outcome": "partial",
      "register_wrong": false,
      "wrong_answer_key": "协议层；错误的第二空",
      "feedback": "部分正确。第一空应填“封装”，但你的回答中包含了额外或错误的信息。完整过程称为“封装”与解封装。"
    },
    {
      "question_id": "Q4",
      "number": 4,
      "type": "简答",
      "score": 2,
      "max_score": 2,
      "outcome": "correct",
      "register_wrong": false,
      "wrong_answer_key": null,
      "feedback": "回答正确。TCP提供面向连接的可靠传输，具备确认、重传等机制；UDP提供无连接的尽力而为传输，时延较小但不保证可靠交付。"
    },
    {
      "question_id": "Q5",
      "number": 5,
      "type": "论述",
      "score": 0,
      "max_score": 2,
      "outcome": "wrong",
      "register_wrong": true,
      "wrong_answer_key": "只讨论模块化，故意遗漏互操作性和故障定位。",
      "feedback": "回答不完整。分层设计的价值在于隔离变化、简化维护及促进互操作性；代价包括处理开销增加和故障定位链条变长。你的回答遗漏了关键点。"
    }
  ]
}"""

# Durable charge record fields preserved in the same evidence record.
EV_PAGE_ID = "3e191b6f-53e1-819d-be1e-fa826feed319"
EV_PAGE_URL = "https://app.notion.com/p/E2E-3e191b6f53e1819dbe1efa826feed319"
EV_OPERATION_ID = "538bbdc725244748aa55e666191afdbd"
EV_GRADED_AT = "2026-09-20T22:03:56+08:00"
EV_CHARGED = 0.036
EV_REMAINING = 99.953


class _Relay:
    """Counting fake relay returning an arbitrary content payload."""

    def __init__(self, content: str, *, balance: float = 100.0):
        self.content = content
        self.calls = 0
        self.balance = balance

    def quota(self) -> dict:
        return {"available": True, "llm_points_remaining": self.balance}

    def grade(self, _prompt: str) -> dict:
        self.calls += 1
        self.balance -= 0.036
        return {"content": self.content, "points_charged": 0.036}


class _NoSpendRelay:
    """A relay that must never be contacted during a durable resume."""

    def quota(self):
        raise AssertionError("durable resume must not read quota from the relay")

    def grade(self, _prompt: str) -> dict:
        raise AssertionError("durable resume must not call the relay for a charged record")


class _ContractAdapter(FakeGradingPageAdapter):
    """Reads the [E2E] strict-contract flag, like the constructed page."""

    def read_for_grading(self, target):
        return GradingInput(
            prompt="strict fixture prompt",
            unanswered=[],
            answer_fingerprint="fixture-answers-v1",
            answer_provenance="known",
            result_contract=GRADE_RESULT_CONTRACT_VERSION,
            result_marker="E2E_GRADE_RESULT",
        )


class _DirectoryStub:
    """Plain durable directory stub (record store + target resolution)."""

    def __init__(self, store: JsonGradingRecordStore):
        self.grading_record_store = store
        self.local_grading_records = store.snapshot()

    def exercise_grade_target(self, exercise_id):
        return {
            "page_id": exercise_id,
            "page_url": f"https://www.notion.so/{exercise_id}",
            "title": "[E2E] 第一讲 · 计算机网络练习",
            "course_id": "3d791b6f-53e1-81a7-a7c1-f379bbde9086",
            "course_title": "计算机网络",
            "scope": "第一讲",
        }

    def record_grading(self, exercise_id, record):
        self.grading_record_store.put(exercise_id, record)
        self.local_grading_records[exercise_id] = copy.deepcopy(record)


def _fresh_grader(content: str):
    directory = build_fake_directory()
    directory.load()
    relay = _Relay(content)
    adapter = _ContractAdapter()
    grader = ExerciseGrader(
        directory_service=directory,
        relay=relay,
        page_adapter=adapter,
        clock=lambda: GRADED_AT,
    )
    return grader, relay, adapter


def _mechanical_echo_result() -> dict:
    return json.loads(VERBATIM_RESPONSE)


def _assert_normalized(written: dict) -> None:
    assert written["contract_version"] == GRADE_RESULT_CONTRACT_VERSION
    assert written["score"] == 50
    assert written["earned_points"] == SUMMED_EARNED == 5
    assert written["max_points"] == 10
    assert [row["question_id"] for row in written["questions"]] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    # Mechanical echo normalized from the per-question question_id.
    assert [row["wrong_answer_key"] for row in written["questions"]] == [
        "Q1", "Q2", "Q3", "Q4", "Q5",
    ]
    assert [row["score"] for row in written["questions"]] == [2, 0, 1, 2, 0]
    assert [row["partial"] for row in written["questions"]] == [
        False, False, True, False, False,
    ]
    assert [row["register_wrong"] for row in written["questions"]] == [
        False, True, False, False, True,
    ]


def test_verbatim_charged_response_is_accepted_and_mechanically_normalized():
    grader, relay, adapter = _fresh_grader(VERBATIM_RESPONSE)

    summary = grader.grade(grader.prepare(EXERCISE_GENERATED))

    assert summary["status"] == "completed"
    assert summary["score"] == 50
    assert summary["points_charged"] == 0.036
    assert summary["points_remaining"] == pytest.approx(99.964)
    assert relay.calls == 1
    assert len(adapter.write_calls) == 1
    written = json.loads(adapter.write_calls[0]["content"])
    _assert_normalized(written)
    # Registration uses the normalized stable per-question keys only.
    assert adapter.wrong_answer_calls == [
        ("dedup", "Q2"), ("create", "Q2"),
        ("dedup", "Q5"), ("create", "Q5"),
    ]


def test_verbatim_response_normalizes_to_pinned_per_question_result():
    validated = _validate_grade_result(VERBATIM_RESPONSE)
    assert validated["score"] == 50
    assert validated["earned_points"] == 5
    assert validated["max_points"] == 10
    q2 = next(row for row in validated["questions"] if row["question_id"] == "Q2")
    assert q2["wrong_answer_key"] == "Q2"
    q1 = next(row for row in validated["questions"] if row["question_id"] == "Q1")
    assert q1["wrong_answer_key"] == "Q1"
    assert q1["score"] == 2 and q2["score"] == 0


def test_response_contract_pins_wrong_answer_key_and_earned_points_clauses():
    directory = build_fake_directory("grade-slow")
    directory.load()
    target = directory.exercise_grade_target(EXERCISE_GENERATED)
    # _grading_prompt is driven by the target title ([E2E] prefix), so reuse
    # the constructed target shape the production adapter builds.
    prepared_target = GradeTarget(
        operation="grade",
        page_id=target["page_id"],
        page_url=target["page_url"],
        title="[E2E] 五题批改契约",
        course_id=target["course_id"],
        course_title=target["course_title"],
        scope=target["scope"],
    )
    prompt, unanswered = _grading_prompt(
        prepared_target, directory.provider.client._ws.children[EXERCISE_GENERATED]
    )
    contract = json.loads(prompt)["response_contract"]
    assert unanswered == []
    assert "wrong_answer_key_rule" in contract
    assert "question_id" in contract["wrong_answer_key_rule"]
    assert "earned_points_rule" in contract
    assert "sum" in contract["earned_points_rule"]
    assert contract["score_formula"] == "earned_points / max_points * 100"
    assert contract["wrong_answer_question_ids"] == ["Q2", "Q5"]


def _substantive_mutations():
    return {
        "missing": lambda value: value["questions"].pop(),
        "duplicate": lambda value: value["questions"][4].update(question_id="Q2"),
        "out-of-range": lambda value: value["questions"][2].update(score=3),
        "wrong-type": lambda value: value["questions"][3].update(type="论述"),
        "wrong-partial-semantics": lambda value: value["questions"][2].update(outcome="wrong"),
        "wrong-registration-semantics": lambda value: value["questions"][2].update(register_wrong=True),
        "wrong-id-type": lambda value: value["questions"][1].update(question_id=[]),
        "non-string-key": lambda value: value["questions"][1].update(wrong_answer_key=[]),
        "bogus-max": lambda value: value.update(max_points=12),
        "earned-type": lambda value: value.update(earned_points="6"),
        "score-type": lambda value: value.update(score=None),
        "empty-feedback": lambda value: value["questions"][0].update(feedback=" "),
    }


@pytest.mark.parametrize("name", list(_substantive_mutations()))
def test_substantive_grade_checks_stay_enforced_after_normalization(name):
    result = _mechanical_echo_result()
    _substantive_mutations()[name](result)
    grader, relay, adapter = _fresh_grader(json.dumps(result, ensure_ascii=False))

    with pytest.raises(GradeSettlementError, match="AI 返回的批改结果不完整") as raised:
        grader.grade(grader.prepare(EXERCISE_GENERATED))

    assert raised.value.points_charged == 0.036 and relay.calls == 1
    assert adapter.write_calls == [] and adapter.wrong_answer_calls == []


def test_malformed_json_still_fails_with_the_invalid_reason():
    with pytest.raises(GradeBlocked, match=GRADE_RESULT_INVALID_REASON):
        _validate_grade_result("{not-json")


def test_durable_zero_spend_resume_completes_retained_charged_record(tmp_path):
    store = JsonGradingRecordStore(tmp_path / "grading_records.json")
    charged = {
        "version": 2, "status": "pending", "phase": "relay_succeeded",
        "operation_id": EV_OPERATION_ID, "operation": "grade",
        "page_id": EV_PAGE_ID, "page_url": EV_PAGE_URL,
        "course_id": "3d791b6f-53e1-81a7-a7c1-f379bbde9086",
        "course_title": "计算机网络",
        "title": "[E2E] 第一讲 · 计算机网络练习", "scope": "第一讲",
        "answer_fingerprint": "831aae89522437fff2177358fb4dd6a853b003255467ce3dfd9979a22aafea1c",
        "answer_provenance": "known", "regrade": False,
        "result_marker": "E2E_GRADE_RESULT",
        "result_contract": GRADE_RESULT_CONTRACT_VERSION,
        "content": VERBATIM_RESPONSE,
        "points_charged": EV_CHARGED, "points_remaining": EV_REMAINING,
        "result_page_url": EV_PAGE_URL, "graded_at": EV_GRADED_AT,
    }
    store.put(EV_PAGE_ID, charged)
    directory = _DirectoryStub(store)
    relay = _NoSpendRelay()
    adapter = FakeGradingPageAdapter()
    grader = ExerciseGrader(
        directory_service=directory,
        relay=relay,
        page_adapter=adapter,
        clock=lambda: GRADED_AT,
    )

    summary = grader.grade(grader.prepare(EV_PAGE_ID))

    # Zero-spend recovery: the charged record completes locally with no relay
    # call (the _NoSpendRelay raises if contacted).
    assert summary["status"] == "completed"
    assert summary["score"] == 50
    assert summary["points_charged"] == EV_CHARGED
    assert summary["points_remaining"] == EV_REMAINING
    assert summary["graded_at"] == EV_GRADED_AT
    assert summary["result_page_url"] == EV_PAGE_URL
    assert len(adapter.write_calls) == 1
    written = json.loads(adapter.write_calls[0]["content"])
    _assert_normalized(written)
    assert adapter.wrong_answer_calls == [
        ("dedup", "Q2"), ("create", "Q2"),
        ("dedup", "Q5"), ("create", "Q5"),
    ]
    record = store.snapshot()[EV_PAGE_ID]
    assert record["status"] == "graded" and record["phase"] == "completed"
    assert record["points_charged"] == EV_CHARGED and "content" not in record
