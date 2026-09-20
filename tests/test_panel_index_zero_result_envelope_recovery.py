"""Regression coverage for index-zero grading-envelope recovery."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import pku_sync.panel.exercise_grader as grader_module
from pku_sync.notion import markdown_to_blocks
from pku_sync.panel.exercise_grader import (
    PHASE_COMPLETED,
    PHASE_RELAY_SUCCEEDED,
    ExerciseGrader,
    GradeSettlementError,
    GradeTarget,
    GradingInput,
    RealGradingPageAdapter,
    _operation_result_markdown,
    _result_append_action,
)
from pku_sync.panel.exercises import JsonGradingRecordStore

CONTENT = json.dumps(
    {
        "score": 100,
        "questions": [
            {"number": 1, "type": "选择", "score": 2, "max_score": 2}
        ],
    },
    ensure_ascii=False,
)
GRADED_AT = "2026-09-20T12:00:00+08:00"


def target() -> GradeTarget:
    return GradeTarget(
        "grade",
        "1" * 32,
        "https://www.notion.so/" + "1" * 32,
        "考前练习",
        "2" * 32,
        "计算机网络",
        "考前练习",
    )


def prepared(*, marker: bool = False) -> tuple[GradeTarget, GradingInput]:
    return target(), GradingInput(
        prompt="grade-prompt",
        unanswered=[],
        marker_present=marker,
        answer_fingerprint="answers-v1",
        answer_provenance="known",
    )


def plan(operation_id: str = "operation") -> list[dict]:
    return markdown_to_blocks(
        _operation_result_markdown(
            operation_id,
            CONTENT,
            score=100,
            graded_at=GRADED_AT,
        )
    )


def with_ids(blocks: list[dict], start: int) -> list[dict]:
    result = copy.deepcopy(blocks)
    for index, block in enumerate(result, start):
        block["id"] = f"{index:032x}"
    return result


@pytest.mark.parametrize("prefix_length", [2, 3, 4])
def test_index_zero_incomplete_exact_envelope_is_always_replaced(prefix_length):
    frozen = plan()
    prefix = with_ids(frozen[:prefix_length], 100)

    action, owned = _result_append_action(
        prefix,
        frozen,
        operation_id="operation",
        marker="批改结果",
    )

    assert action == "replace-prefix"
    assert owned == prefix


class Relay:
    def __init__(self):
        self.calls = 0
        self.balance = 20.0

    def quota(self):
        return {"available": True, "llm_points_remaining": self.balance}

    def grade(self, _prompt):
        self.calls += 1
        self.balance -= 3.0
        return {"content": CONTENT, "points_charged": 3.0}


class Directory:
    def __init__(self, path: Path):
        self.grading_record_store = JsonGradingRecordStore(path)
        self.local_grading_records = self.grading_record_store.snapshot()

    def record_grading(self, exercise_id, record):
        self.grading_record_store.put(exercise_id, record)
        self.local_grading_records[exercise_id] = copy.deepcopy(record)


class InterruptedPageClient:
    """Commit an index-zero prefix, then interrupt its first retirement."""

    def __init__(self):
        self.blocks: list[dict] = []
        self.appended: list[list[dict]] = []
        self.successful_archives: list[str] = []
        self.old_owned_ids: set[str] = set()
        self._interrupt_append = True
        self._interrupt_retirement = True
        self._insert_during_retirement = True
        self.inserted_answers = with_ids(
            markdown_to_blocks("### 第 1 题\n\n答案：学生原始答案"), 700
        )
        self.inserted_unrelated = with_ids(
            markdown_to_blocks("### 并发补充\n\n不要删除这段用户内容"), 800
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def list_children(self, _page_id):
        return copy.deepcopy(self.blocks)

    def append_blocks(self, _page_id, blocks, **_kwargs):
        self.appended.append(copy.deepcopy(blocks))
        if self._interrupt_append:
            self._interrupt_append = False
            partial = with_ids(blocks[:4], 100)
            self.blocks.extend(partial)
            self.old_owned_ids = {str(block["id"]) for block in partial}
            raise OSError("append response lost after an index-zero partial commit")

        assert self.old_owned_ids.isdisjoint(
            str(block.get("id") or "") for block in self.blocks
        ), "a full append started before every selected owned block was absent"
        created = with_ids(blocks, 1000 + len(self.blocks))
        self.blocks.extend(created)
        return created

    def archive_block(self, block_id, **_kwargs):
        if self._insert_during_retirement:
            self._insert_during_retirement = False
            self.blocks.extend(copy.deepcopy(self.inserted_answers))
            self.blocks.extend(copy.deepcopy(self.inserted_unrelated))
        if self._interrupt_retirement and self.successful_archives:
            self._interrupt_retirement = False
            raise OSError("retirement response failed before commit")
        self.blocks = [
            block for block in self.blocks if str(block.get("id") or "") != block_id
        ]
        self.successful_archives.append(block_id)
        return {"archived": True}


def restart(path: Path, relay: Relay) -> ExerciseGrader:
    return ExerciseGrader(
        directory_service=Directory(path),
        relay=relay,
        page_adapter=RealGradingPageAdapter(object()),
        clock=lambda: GRADED_AT,
    )


def test_index_zero_append_and_retirement_interruptions_converge_after_restart(
    tmp_path, monkeypatch
):
    record_path = tmp_path / "grading.json"
    relay = Relay()
    client = InterruptedPageClient()
    monkeypatch.setattr(grader_module, "get_client", lambda _settings: client)

    with pytest.raises(GradeSettlementError):
        restart(record_path, relay).grade(prepared())
    first_record = JsonGradingRecordStore(record_path).snapshot()[target().page_id]
    frozen = first_record["append_plan"]
    partial_ids = [str(block["id"]) for block in client.blocks]
    assert first_record["phase"] == PHASE_RELAY_SUCCEEDED
    assert len(client.blocks) == 4
    assert relay.calls == 1

    with pytest.raises(GradeSettlementError):
        restart(record_path, relay).grade(prepared(marker=True))
    assert client.successful_archives == [partial_ids[0]]
    assert relay.calls == 1

    completed = restart(record_path, relay).grade(prepared(marker=True))

    assert completed["status"] == "completed"
    assert completed["points_charged"] == 3.0
    assert completed["points_remaining"] == 17.0
    assert relay.calls == 1
    assert JsonGradingRecordStore(record_path).snapshot()[target().page_id][
        "phase"
    ] == PHASE_COMPLETED
    assert client.successful_archives == [
        partial_ids[0],
        partial_ids[3],
        partial_ids[2],
        partial_ids[1],
    ]
    assert client.appended == [frozen, frozen]

    final_signatures = [grader_module._block_signature(block) for block in client.blocks]
    frozen_signatures = [grader_module._block_signature(block) for block in frozen]
    assert final_signatures[-len(frozen_signatures) :] == frozen_signatures
    assert all(block in client.blocks for block in client.inserted_answers)
    assert all(block in client.blocks for block in client.inserted_unrelated)
    assert len(
        [
            block
            for block in client.blocks
            if grader_module._plain(block)
            == "PKU_GRADE_OPERATION:" + first_record["operation_id"]
        ]
    ) == 1
    assert len(
        [block for block in client.blocks if grader_module._plain(block) == "批改结果"]
    ) == 1
    assert not any(
        grader_module._looks_app_owned_result_block(block)
        for block in client.blocks[: -len(frozen)]
    )
