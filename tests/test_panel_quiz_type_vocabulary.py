from __future__ import annotations

import json

import pytest

from pku_sync.panel.exercise_organizer import (
    ExerciseOrganizer,
    MemoryOrganizeRecordStore,
    OrganizeBlocked,
    QUESTION_TYPES,
    _parse_quiz,
    _prompt,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L1, build_panel_workspace


def _quiz_payload(types: list[str]) -> str:
    return json.dumps(
        {
            "title": "第一讲 · 计算机网络练习",
            "questions": [
                {
                    "type": question_type,
                    "question": f"{question_type}相关问题",
                    "source": "第一讲",
                    "answer": f"{question_type}答案",
                }
                for question_type in types
            ],
        },
        ensure_ascii=False,
    )


def test_parse_quiz_normalizes_mixed_bare_and_trailing_suffix_types():
    _title, questions = _parse_quiz(
        _quiz_payload(["选择", "判断题", "填空", "简答题", "论述"])
    )

    assert [item["type"] for item in questions] == ["选择", "判断", "填空", "简答", "论述"]
    assert {item["type"] for item in questions} == QUESTION_TYPES


def test_mixed_bare_and_trailing_suffix_types_complete_organize():
    class Relay:
        def quota(self) -> dict:
            return {"available": True, "llm_points_remaining": 20.0}

        def quiz(self, prompt: str) -> dict:
            return {
                "content": _quiz_payload(
                    ["选择题", "判断", "填空题", "简答", "论述题"]
                ),
                "points_charged": 2.0,
            }

    class Pages:
        def __init__(self):
            self.children: list[dict] = []

        def create_page(self, parent_page_id: str, title: str, *, children: list[dict]):
            self.children = children
            return {"id": "exercise-1", "url": "https://notion.so/exercise-1"}

    class Notes:
        def for_scope(self, *, course_title: str, lecture_titles: list[str]) -> list[dict]:
            return [{"title": lecture_titles[0], "source": "课堂录像笔记", "notes": "notes"}]

    pages = Pages()
    organizer = ExerciseOrganizer(
        directory_service=build_fake_directory(workspace=build_panel_workspace()),
        relay=Relay(),
        page_adapter=pages,
        notes_provider=Notes(),
        record_store=MemoryOrganizeRecordStore(),
    )

    result = organizer.organize(COURSE_NET, [NET_L1])

    assert result["status"] == "completed"
    rendered = json.dumps(pages.children, ensure_ascii=False)
    assert all(question_type in rendered for question_type in QUESTION_TYPES)


def test_prompt_pins_exact_bare_type_vocabulary():
    prompt = _prompt(
        "计算机网络",
        ["第一讲"],
        [{"title": "第一讲", "source": "课堂录像笔记", "notes": "分层与端到端"}],
    )

    assert (
        "JSON 的 type 字段必须严格是以下五个值之一：选择、判断、填空、简答、论述"
        in prompt
    )
    assert "不得添加“题”后缀" in prompt


def test_parse_quiz_rejects_repeated_types_before_diversity_completes():
    with pytest.raises(OrganizeBlocked) as blocked:
        _parse_quiz(_quiz_payload(["选择", "选择", "填空", "简答", "论述"]))

    assert blocked.value.status_code == 502
    assert blocked.value.reason == "AI 返回的题型不够丰富，请重试。"


@pytest.mark.parametrize(
    "invalid_type",
    ["未知", "选择题题", "", "   ", "选择 题", "题"],
)
def test_parse_quiz_rejects_unrelated_empty_and_multiply_suffixed_types(
    invalid_type: str,
):
    types = ["选择", "判断", "填空", "简答", "论述"]
    types[0] = invalid_type

    with pytest.raises(OrganizeBlocked) as blocked:
        _parse_quiz(_quiz_payload(types))

    assert blocked.value.status_code == 502
