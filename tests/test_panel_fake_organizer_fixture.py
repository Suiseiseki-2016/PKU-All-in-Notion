import json

from fastapi.testclient import TestClient

from pku_sync.panel.exercise_organizer import ExerciseOrganizer, MemoryOrganizeRecordStore
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L1, build_panel_workspace
from pku_sync.panel.platform_bridge import FakePlatformBridge
from pku_sync.panel.webapi import create_app


class FakePageAdapter:
    def __init__(self):
        self.calls = []

    def create_page(self, parent_page_id, title, *, children):
        self.calls.append({"parent": parent_page_id, "title": title, "children": children})
        return {
            "id": "2c000001-0000-4000-8000-000000000901",
            "url": "https://www.notion.so/2c000001000040008000000000000901",
            "last_edited_time": "2026-09-19T08:00:00.000Z",
        }


class FakeNotes:
    def for_scope(self, *, course_title, lecture_titles):
        return [{"title": lecture_titles[0], "source": "课堂录像笔记", "notes": "私有笔记正文"}]


def test_fake_platform_bridge_fixture_organizes_all_question_types_under_course():
    directory = build_fake_directory(workspace=build_panel_workspace())
    relay = FakePlatformBridge(activated=True, llm_points=20)
    adapter = FakePageAdapter()
    organizer = ExerciseOrganizer(
        directory_service=directory,
        relay=relay,
        page_adapter=adapter,
        notes_provider=FakeNotes(),
        record_store=MemoryOrganizeRecordStore(),
    )
    client = TestClient(create_app(directory_service=directory, platform_service=relay,
                                   organizer_service=organizer))

    result = organizer.organize(COURSE_NET, [NET_L1])

    assert result["status"] == "completed"
    assert result["points_charged"] == 3.0
    assert result["points_remaining"] == 17.0
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["parent"] == COURSE_NET

    page_text = "\n".join(
        part.get("text", {}).get("content", "")
        for block in adapter.calls[0]["children"]
        for part in block[block["type"]].get("rich_text", [])
    )
    assert all(kind in page_text for kind in ("选择", "判断", "填空", "简答", "论述"))

    questions = json.loads(relay.quiz("fixture")["content"])["questions"]
    assert [item["type"] for item in questions] == ["选择", "判断", "填空", "简答", "论述"]
    assert all(item["source"].strip() and item["answer"].strip() for item in questions)

    row = next(item for item in client.get("/api/exercises").json()["exercises"]
               if item["id"] == result["exercise_id"])
    assert row["status_label"] == "已整理"
