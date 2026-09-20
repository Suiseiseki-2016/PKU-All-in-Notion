from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from pku_sync.panel.exercise_organizer import (
    ExerciseOrganizer,
    MemoryOrganizeRecordStore,
    OrganizeBlocked,
    QUESTION_TYPES,
    _prompt,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L1, build_panel_workspace
from pku_sync.panel.webapi import create_app


class FakeRelay:
    def __init__(self, title: str):
        self.title = title
        self.calls: list[str] = []

    def quota(self) -> dict:
        return {"available": True, "llm_points_remaining": 20.0}

    def quiz(self, prompt: str) -> dict:
        self.calls.append(prompt)
        questions = [
            {
                "type": question_type,
                "question": f"{question_type}题",
                "source": "第一讲",
                "answer": f"{question_type}答案",
            }
            for question_type in sorted(QUESTION_TYPES)
        ]
        return {
            "content": json.dumps(
                {"title": self.title, "questions": questions}, ensure_ascii=False
            ),
            "points_charged": 2.0,
        }


class FakePages:
    def __init__(self):
        self.calls: list[dict] = []

    def create_page(self, parent_page_id: str, title: str, *, children: list[dict]):
        self.calls.append(
            {"parent_page_id": parent_page_id, "title": title, "children": children}
        )
        number = len(self.calls)
        return {
            "id": f"2c000001-0000-4000-8000-{number:012d}",
            "url": f"https://www.notion.so/e2e-organizer-{number}",
            "last_edited_time": "2026-09-20T08:00:00.000Z",
        }


class FakeNotes:
    def for_scope(self, *, course_title: str, lecture_titles: list[str]) -> list[dict]:
        return [
            {
                "title": lecture_titles[0],
                "source": "课堂录像笔记",
                "notes": "有效的 UTF-8 课堂笔记。",
            }
        ]


def make_organizer(title: str):
    directory = build_fake_directory(workspace=build_panel_workspace())
    relay = FakeRelay(title)
    pages = FakePages()
    organizer = ExerciseOrganizer(
        directory_service=directory,
        relay=relay,
        page_adapter=pages,
        notes_provider=FakeNotes(),
        record_store=MemoryOrganizeRecordStore(),
        e2e_enabled=True,
    )
    return organizer, relay, pages


def wait_for_result(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        response = client.get(
            "/api/exercises/organize/status", params={"job_id": job_id}
        )
        payload = response.json()
        if payload.get("status") != "running":
            return payload
        time.sleep(0.005)
    raise AssertionError("organize job did not finish")


def test_prompt_is_valid_utf8_and_pins_the_exact_five_type_source_contract():
    prompt = _prompt(
        "计算机网络",
        ["第一讲"],
        [{"title": "第一讲", "source": "课堂录像笔记", "notes": "分层与端到端"}],
    )

    assert prompt.encode("utf-8").decode("utf-8") == prompt
    assert "各有且仅有一道选择、判断、填空、简答、论述题" in prompt
    assert "每题必须带来源" in prompt
    assert "{title, questions:[{type,question,source,answer}]}" in prompt
    for mojibake_fragment in ("è¯", "æ ", "é", "ï¼"):
        assert mojibake_fragment not in prompt


def test_e2e_mode_prefixes_recognized_and_normalized_titles_at_create_boundary():
    cases = (
        ("第一讲 · 计算机网络练习", "[E2E] 第一讲 · 计算机网络练习"),
        ("第一讲知识检测", "[E2E] 练习 · 第一讲知识检测"),
        ("[E2E] 第一讲 · 计算机网络练习", "[E2E] 第一讲 · 计算机网络练习"),
        ("[E2E] [E2E] 第一讲 · 计算机网络练习", "[E2E] 第一讲 · 计算机网络练习"),
    )

    for provider_title, expected_title in cases:
        organizer, _relay, pages = make_organizer(provider_title)
        result = organizer.organize(COURSE_NET, [NET_L1], e2e_mode=True)

        assert pages.calls[0]["title"] == expected_title
        assert pages.calls[0]["title"].count("[E2E]") == 1
        assert result["title"] == expected_title


def test_production_mode_preserves_titles_without_accidental_e2e_prefix():
    for provider_title, expected_title in (
        ("第一讲 · 计算机网络练习", "第一讲 · 计算机网络练习"),
        ("第一讲知识检测", "练习 · 第一讲知识检测"),
        ("[E2E] 第一讲 · 计算机网络练习", "第一讲 · 计算机网络练习"),
    ):
        organizer, _relay, pages = make_organizer(provider_title)
        result = organizer.organize(COURSE_NET, [NET_L1])

        assert pages.calls[0]["title"] == expected_title
        assert result["title"] == expected_title
        assert "[E2E]" not in pages.calls[0]["title"]


def test_e2e_mode_is_off_by_default_and_blocks_before_relay_or_page_creation():
    directory = build_fake_directory(workspace=build_panel_workspace())
    relay = FakeRelay("第一讲 · 计算机网络练习")
    pages = FakePages()
    organizer = ExerciseOrganizer(
        directory_service=directory,
        relay=relay,
        page_adapter=pages,
        notes_provider=FakeNotes(),
        record_store=MemoryOrganizeRecordStore(),
    )

    try:
        organizer.organize(COURSE_NET, [NET_L1], e2e_mode=True)
    except OrganizeBlocked as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("disabled E2E mode should be rejected")
    assert relay.calls == []
    assert pages.calls == []


def test_e2e_and_production_scopes_are_distinct_but_each_mode_is_idempotent():
    organizer, relay, pages = make_organizer("第一讲 · 计算机网络练习")

    production = organizer.organize(COURSE_NET, [NET_L1])
    production_retry = organizer.organize(COURSE_NET, [NET_L1])
    e2e = organizer.organize(COURSE_NET, [NET_L1], e2e_mode=True)
    e2e_retry = organizer.organize(COURSE_NET, [NET_L1], e2e_mode=True)

    assert production_retry["exercise_id"] == production["exercise_id"]
    assert e2e_retry["exercise_id"] == e2e["exercise_id"]
    assert production["exercise_id"] != e2e["exercise_id"]
    assert len(relay.calls) == 2
    assert len(pages.calls) == 2
    assert pages.calls[0]["title"] == "第一讲 · 计算机网络练习"
    assert pages.calls[1]["title"] == "[E2E] 第一讲 · 计算机网络练习"


def test_api_passes_explicit_e2e_mode_through_the_organize_path():
    class SpyOrganizer:
        def __init__(self):
            self.calls: list[dict] = []

        def estimate(self) -> dict:
            return {"label": "estimate", "min_points": 1, "max_points": 5}

        def organize(
            self, course_id: str, lecture_ids: list[str], *, e2e_mode: bool = False
        ) -> dict:
            self.calls.append(
                {
                    "course_id": course_id,
                    "lecture_ids": lecture_ids,
                    "e2e_mode": e2e_mode,
                }
            )
            return {"status": "completed", "exercise_id": "fixture"}

    organizer = SpyOrganizer()
    client = TestClient(create_app(organizer_service=organizer))
    started = client.post(
        "/api/exercises/organize",
        json={
            "course_id": COURSE_NET,
            "lecture_ids": [NET_L1],
            "e2e_mode": True,
        },
    )

    assert started.status_code == 202
    result = wait_for_result(client, started.json()["job_id"])
    assert result["status"] == "completed"
    assert organizer.calls == [
        {
            "course_id": COURSE_NET,
            "lecture_ids": [NET_L1],
            "e2e_mode": True,
        }
    ]
