"""Additive grading-contract cases missed by the initial core coverage."""

from __future__ import annotations

from pathlib import Path

from pku_sync.panel.quiz_grading_core import GradeTarget, _grading_prompt
from pku_sync.panel.student_ui import PANEL_COPY


APP_JS = Path(__file__).parents[1] / "pku_sync" / "panel" / "static" / "app.js"


def _rich_text(text: str) -> list[dict]:
    return [{"type": "text", "plain_text": text, "text": {"content": text}}]


def _block(kind: str, text: str) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich_text(text)}}


def _target() -> GradeTarget:
    return GradeTarget(
        operation="grade",
        page_id="exercise-page",
        page_url="https://www.notion.so/exercise-page",
        title="考前练习",
        course_id="course-page",
        course_title="计算机网络",
        scope="考前练习",
    )




def test_production_answer_scan_reports_a_blank_middle_question():
    blocks = [
        _block("heading_3", "第 1 题"), _block("paragraph", "答案：A"),
        _block("heading_3", "第 2 题"), _block("paragraph", "答案："),
        _block("heading_3", "第 3 题"), _block("paragraph", "答案：C"),
        _block("heading_2", "教师区（答案）"),
        _block("numbered_list_item", "1. 答案：A"),
        _block("numbered_list_item", "2. 答案：B"),
        _block("numbered_list_item", "3. 答案：C"),
    ]
    _, unanswered = _grading_prompt(_target(), blocks)
    assert unanswered == ["第 2 题"]


def test_e2e_answer_scan_treats_number_only_items_as_unanswered():
    blocks = [
        _block("heading_3", "第 1 题"), _block("heading_3", "第 2 题"),
        _block("heading_3", "第 3 题"), _block("heading_2", "E2E_ANSWER_FIXTURE"),
        _block("numbered_list_item", "1. answer-1"),
        _block("numbered_list_item", "2."),
        _block("numbered_list_item", "3. answer-3"),
    ]
    _, unanswered = _grading_prompt(_target(), blocks)
    assert unanswered == ["第 2 题"]
