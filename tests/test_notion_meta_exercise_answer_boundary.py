"""Additive coverage for organizer teacher-answer boundaries."""

from __future__ import annotations

from pku_sync.notion import markdown_to_blocks
from pku_sync.notion_meta.exercises import scan_exercise_signals
from pku_sync.panel.exercises import (
    EXERCISE_ORGANIZED,
    STATUS_LABELS,
    derive_exercise_state,
)


def _notion_read_blocks(markdown: str) -> list[dict]:
    """Add response-only plain_text fields to organizer write-shaped blocks."""
    blocks = markdown_to_blocks(markdown)
    for block in blocks:
        kind = block["type"]
        for piece in block[kind].get("rich_text", []):
            piece["plain_text"] = piece["text"]["content"]
    return blocks


def _organizer_blocks() -> list[dict]:
    """Return the real block shape emitted by the exercise organizer."""
    return _notion_read_blocks(
        """\
## 练习题

### 1. 选择

下列哪项正确？

来源：第一讲

### 2. 判断

该说法是否正确？

来源：第二讲

### 3. 填空

请填写两个空。

来源：第三讲

### 4. 简答

请简要说明理由。

来源：第四讲

### 5. 论述

请结合课程内容作答。

来源：第五讲

## 教师区（答案）

1. 答案：B

2. 答案：正确

3. 答案：协议层；可靠传输

4. 答案：这是标准答案。

5. 答案：这是论述题标准答案。
"""
    )


def test_organizer_teacher_answers_do_not_mark_fresh_page_answered():
    blocks = _organizer_blocks()

    marker_present, answer_present = scan_exercise_signals(blocks)

    assert marker_present is False
    assert answer_present is False
    state = derive_exercise_state(
        marker_present=marker_present,
        answer_present=answer_present,
        launch_started=False,
    )
    assert state == EXERCISE_ORGANIZED
    assert STATUS_LABELS[state] == "已整理"


def test_later_grade_heading_remains_scannable_after_teacher_boundary():
    blocks = _organizer_blocks() + _notion_read_blocks(
        """\
## 批改结果

总分：5/10
"""
    )

    assert scan_exercise_signals(blocks) == (True, False)


def test_later_e2e_answer_fixture_remains_scannable_after_teacher_boundary():
    blocks = _organizer_blocks() + _notion_read_blocks(
        """\
## E2E_ANSWER_FIXTURE

1. student-answer-1

2. student-answer-2
"""
    )

    assert scan_exercise_signals(blocks) == (False, True)


def test_later_grade_and_answer_markers_are_both_scannable():
    blocks = _organizer_blocks() + _notion_read_blocks(
        """\
## E2E_GRADE_RESULT

## E2E_ANSWER_FIXTURE

1. student-answer-1
"""
    )

    assert scan_exercise_signals(blocks) == (True, True)
