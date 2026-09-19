"""Additive grading-contract cases missed by the initial core coverage."""

from __future__ import annotations

from pathlib import Path

from pku_sync.panel.exercise_grader import GradeTarget, _grading_prompt
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


def test_loader_uses_the_approved_explanation_copy():
    assert PANEL_COPY["directory"]["exercises"]["grading"]["explanation"] == (
        "后端正在读取 Notion 中的答案，按需调用 AI 评分并写回解析。请稍候。"
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


def test_completed_grade_applies_the_server_settlement_to_client_balance():
    script = APP_JS.read_text(encoding="utf-8")
    assert "state.quota.llm_points_remaining = next.body.points_remaining" in script


def test_active_grading_replaces_the_dashboard_content():
    script = APP_JS.read_text(encoding="utf-8")
    assert 'if (state.grading.status !== "idle") return gradingScreen();' in script
    start = script.index("function gradingScreen()")
    end = script.index("\n  }", start)
    screen = script[start:end]
    assert "gradingCard()" in screen
    for unrelated in ("statsSection()", "organizeCard()", "exerciseDirectorySection()", "courseGrid()", "activitySection()"):
        assert unrelated not in screen