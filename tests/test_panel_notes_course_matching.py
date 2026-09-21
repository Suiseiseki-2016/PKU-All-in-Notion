"""Real-data-root coverage for LocalNotesProvider course-title matching.

Window C (first attended run) proved the product gap: local PKU course.json
names carry a trailing semester suffix ("计算机网络(26-27学年第1学期)")
while Notion course pages use the plain title ("计算机网络"), so the strict
equality in ``LocalNotesProvider.for_scope`` never matched real synced data
and organize always blocked with 所选范围没有可用课堂笔记.

These tests pin the guarded normalized resolution (precedent:
``submit.resolve_course_id`` — guarded name resolution that refuses
ambiguity) against a tmp_path data root built like a real synced tree:

- exact local course names still win, unchanged;
- one suffixed local course matches the plain Notion title (half- and
  full-width parens) when it is the unique normalized candidate;
- zero or multiple normalized candidates mean no notes — never notes from a
  possibly-wrong course — surfaced through the honest existing block;
- the lecture-title intersection requirement in for_scope stays;
- colliding recording titles across courses never leak across courses.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pku_sync.models import Recording
from pku_sync.panel.exercise_organizer import (
    ExerciseOrganizer,
    LocalNotesProvider,
    MemoryOrganizeRecordStore,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L1, build_panel_workspace
from pku_sync.panel.platform_bridge import FakePlatformBridge
from pku_sync.panel.webapi import create_app
from pku_sync.store import safe_name

# The plain Notion course-page title and lecture titles in the seeded fake
# workspace (the Notion side of the product wiring).
NOTION_NET_TITLE = "计算机网络"
NOTION_NET_LECTURE = "第一讲 · 计算机网络（2026-09-08 第1-2节）"
NOTION_NET_LECTURE_2 = "第二讲 · 计算机网络（2026-09-15 第3-4节）"

NET_NOTES = "计算机网络第一讲笔记正文：分层模型与端到端原则。"
NET_NOTES_2 = "计算机网络第二讲笔记正文：可靠传输与拥塞控制。"
NET_ALT_NOTES = "另一门计算机网络课程的笔记正文，绝不能被选中。"
DEV_NOTES = "发展心理学第一讲笔记正文：皮亚杰认知发展阶段。"

SUFFIXED_HALF = "计算机网络(26-27学年第1学期)"
SUFFIXED_HALF_OLD = "计算机网络(25-26学年第2学期)"
SUFFIXED_FULL = "计算机网络（26-27学年第1学期）"


def seed_course(root: Path, folder: str, name: str, recordings: list[dict] | None) -> Path:
    """Seed one synced course like a real ``sync`` output tree.

    ``recordings`` entries are ``{"title": ..., "notes": ...}``; a recording
    without ``notes`` stays at the indexed stage, and ``recordings=None``
    leaves the course with a course.json and no recordings at all.
    """
    course_dir = root / folder
    course_dir.mkdir(parents=True, exist_ok=True)
    (course_dir / "course.json").write_text(
        json.dumps({"name": name, "course_id": folder}, ensure_ascii=False), encoding="utf-8"
    )
    if recordings is None:
        return course_dir
    (course_dir / "recordings").mkdir(exist_ok=True)
    entries = []
    for item in recordings:
        entry = {
            "course_id": folder,
            "title": item["title"],
            "recorded_at": item.get("recorded_at", "2026-09-08 08:00:00"),
        }
        entries.append(entry)
        if item.get("notes"):
            recording = Recording.model_validate(entry)
            directory = course_dir / "recordings" / safe_name(recording.slug, "recording")
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "notes.md").write_text(item["notes"], encoding="utf-8")
    (course_dir / "recordings" / "index.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )
    return course_dir


def notes_texts(notes: list[dict]) -> list[str]:
    return [item["notes"] for item in notes]


# -- for_scope against a real data root --------------------------------------


def test_exact_course_name_match_wins_over_normalized_candidates(tmp_path: Path):
    seed_course(tmp_path, "net-plain", NOTION_NET_TITLE,
                [{"title": NOTION_NET_LECTURE, "notes": NET_NOTES}])
    seed_course(tmp_path, "net-suffixed", SUFFIXED_HALF,
                [{"title": NOTION_NET_LECTURE, "notes": NET_ALT_NOTES}])
    notes = LocalNotesProvider(tmp_path).for_scope(
        course_title=NOTION_NET_TITLE, lecture_titles=[NOTION_NET_LECTURE]
    )
    assert notes_texts(notes) == [NET_NOTES]


def test_exact_course_without_notes_refuses_normalized_fallback(tmp_path: Path):
    seed_course(tmp_path, "net-plain", NOTION_NET_TITLE,
                [{"title": NOTION_NET_LECTURE}])
    seed_course(tmp_path, "net-suffixed", SUFFIXED_HALF,
                [{"title": NOTION_NET_LECTURE, "notes": NET_ALT_NOTES}])
    notes = LocalNotesProvider(tmp_path).for_scope(
        course_title=NOTION_NET_TITLE, lecture_titles=[NOTION_NET_LECTURE]
    )
    assert notes == []


def test_suffixed_local_course_matches_plain_notion_title(tmp_path: Path):
    seed_course(tmp_path, "net-26", SUFFIXED_HALF,
                [{"title": NOTION_NET_LECTURE, "notes": NET_NOTES}])
    provider = LocalNotesProvider(tmp_path)
    notes = provider.for_scope(
        course_title=NOTION_NET_TITLE, lecture_titles=[NOTION_NET_LECTURE]
    )
    assert notes_texts(notes) == [NET_NOTES]
    assert notes[0]["title"] == NOTION_NET_LECTURE
    assert notes[0]["source"] == "课堂录像笔记"
    # The lecture-title intersection requirement stays: another lecture's
    # scope must not pick this recording up.
    other = provider.for_scope(
        course_title=NOTION_NET_TITLE, lecture_titles=[NOTION_NET_LECTURE_2]
    )
    assert other == []


def test_ambiguous_normalized_courses_yield_no_notes(tmp_path: Path):
    seed_course(tmp_path, "net-26", SUFFIXED_HALF,
                [{"title": NOTION_NET_LECTURE, "notes": NET_NOTES}])
    seed_course(tmp_path, "net-25", SUFFIXED_HALF_OLD,
                [{"title": NOTION_NET_LECTURE, "notes": NET_ALT_NOTES}])
    notes = LocalNotesProvider(tmp_path).for_scope(
        course_title=NOTION_NET_TITLE, lecture_titles=[NOTION_NET_LECTURE]
    )
    assert notes == []


def test_course_without_recordings_counts_as_ambiguity_candidate(tmp_path: Path):
    seed_course(tmp_path, "net-26", SUFFIXED_HALF,
                [{"title": NOTION_NET_LECTURE, "notes": NET_NOTES}])
    seed_course(tmp_path, "net-25", SUFFIXED_HALF_OLD, None)
    notes = LocalNotesProvider(tmp_path).for_scope(
        course_title=NOTION_NET_TITLE, lecture_titles=[NOTION_NET_LECTURE]
    )
    assert notes == []


def test_no_wrong_course_leak_when_recording_titles_collide(tmp_path: Path):
    seed_course(tmp_path, "net-26", SUFFIXED_HALF,
                [{"title": "第一讲", "notes": NET_NOTES}])
    seed_course(tmp_path, "dev-26", "发展心理学(26-27学年第1学期)",
                [{"title": "第一讲", "notes": DEV_NOTES}])
    provider = LocalNotesProvider(tmp_path)
    net_notes = provider.for_scope(course_title="计算机网络", lecture_titles=["第一讲"])
    dev_notes = provider.for_scope(course_title="发展心理学", lecture_titles=["第一讲"])
    assert notes_texts(net_notes) == [NET_NOTES]
    assert notes_texts(dev_notes) == [DEV_NOTES]


def test_full_width_semester_suffix_is_normalized(tmp_path: Path):
    seed_course(tmp_path, "net-26-full", SUFFIXED_FULL,
                [{"title": NOTION_NET_LECTURE, "notes": NET_NOTES}])
    notes = LocalNotesProvider(tmp_path).for_scope(
        course_title=NOTION_NET_TITLE, lecture_titles=[NOTION_NET_LECTURE]
    )
    assert notes_texts(notes) == [NET_NOTES]


@pytest.mark.parametrize("name", [
    "计算机网络(26-27学年第1学期)复习",  # suffix is not trailing
    "计算机网络（26-27学年第1学期）班",  # full-width suffix not trailing
    "计算机网络26-27学年第1学期",  # no parens at all
    "(计算机网络)",  # leading paren, nothing after it to match
    "计算机网络(26-27学年第1学期)(补)",  # only one trailing group is stripped
])
def test_normalization_strips_only_one_trailing_parenthesized_term(tmp_path: Path, name: str):
    seed_course(tmp_path, "net-odd", name,
                [{"title": NOTION_NET_LECTURE, "notes": NET_NOTES}])
    notes = LocalNotesProvider(tmp_path).for_scope(
        course_title=NOTION_NET_TITLE, lecture_titles=[NOTION_NET_LECTURE]
    )
    assert notes == []


def test_zero_candidates_means_no_notes(tmp_path: Path):
    seed_course(tmp_path, "other", "人工智能(26-27学年第1学期)",
                [{"title": NOTION_NET_LECTURE, "notes": NET_NOTES}])
    notes = LocalNotesProvider(tmp_path).for_scope(
        course_title=NOTION_NET_TITLE, lecture_titles=[NOTION_NET_LECTURE]
    )
    assert notes == []


def test_notes_ready_stage_requirement_stays(tmp_path: Path):
    # A suffixed unique candidate whose recording has no notes.md yet stays
    # at the indexed stage: no notes, exactly like today.
    seed_course(tmp_path, "net-26", SUFFIXED_HALF, [{"title": NOTION_NET_LECTURE}])
    notes = LocalNotesProvider(tmp_path).for_scope(
        course_title=NOTION_NET_TITLE, lecture_titles=[NOTION_NET_LECTURE]
    )
    assert notes == []


# -- the real provider through the organize flow (fake Notion directory) ------


@dataclass
class FakeRelay:
    balance: float = 20.0
    points_charged: float = 3.0

    def __post_init__(self):
        self.calls: list[dict] = []

    def quota(self) -> dict:
        return {"active": True, "available": True, "llm_points_remaining": self.balance,
                "transcribe_seconds_remaining": 3600}

    def quiz(self, prompt: str) -> dict:
        self.calls.append({"operation": "quiz", "prompt": prompt})
        self.balance -= self.points_charged
        return {"content": json.dumps({"title": "第一讲 · 计算机网络练习", "questions": [
            {"type": "选择", "question": "选择题", "source": "第一讲", "answer": "A"},
            {"type": "判断", "question": "判断题", "source": "第一讲", "answer": "正确"},
            {"type": "填空", "question": "填空题", "source": "第一讲", "answer": "分层"},
            {"type": "简答", "question": "简答题", "source": "第一讲", "answer": "端到端"},
            {"type": "论述", "question": "论述题", "source": "第一讲", "answer": "按层分析"},
        ]}, ensure_ascii=False), "points_charged": self.points_charged}


class FakePageAdapter:
    def __init__(self):
        self.calls: list[dict] = []

    def create_page(self, parent_page_id: str, title: str, *, children: list[dict]):
        self.calls.append({"parent": parent_page_id, "title": title, "children": children})
        return {"id": "2c000001-0000-4000-8000-000000000901",
                "url": "https://www.notion.so/2c000001000040008000000000000901",
                "last_edited_time": "2026-09-21T08:00:00.000Z"}


def make_client(data_root: Path):
    directory = build_fake_directory(workspace=build_panel_workspace())
    relay = FakeRelay()
    adapter = FakePageAdapter()
    organizer = ExerciseOrganizer(directory_service=directory, relay=relay,
        page_adapter=adapter, notes_provider=LocalNotesProvider(data_root),
        record_store=MemoryOrganizeRecordStore())
    platform_bridge = FakePlatformBridge(activated=True, llm_points=relay.balance)
    app = create_app(directory_service=directory, platform_service=platform_bridge,
                     organizer_service=organizer)
    return TestClient(app), relay, adapter


def organize(client: TestClient):
    started = client.post("/api/exercises/organize", json={
        "course_id": COURSE_NET, "lecture_ids": [NET_L1],
    })
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    for _ in range(200):
        result = client.get("/api/exercises/organize/status", params={"job_id": job_id})
        if result.json().get("status") != "running":
            return result
        time.sleep(0.005)
    pytest.fail("organize job did not finish")


def test_organize_flow_exact_course_data_still_completes(tmp_path: Path):
    seed_course(tmp_path, "net-plain", NOTION_NET_TITLE,
                [{"title": NOTION_NET_LECTURE, "notes": NET_NOTES}])
    client, relay, adapter = make_client(tmp_path)
    response = organize(client)
    assert response.json()["status"] == "completed"
    assert NET_NOTES in relay.calls[0]["prompt"]
    assert adapter.calls[0]["parent"] == COURSE_NET


def test_organize_flow_finds_notes_from_suffixed_real_course_data(tmp_path: Path):
    seed_course(tmp_path, "net-26", SUFFIXED_HALF,
                [{"title": NOTION_NET_LECTURE, "notes": NET_NOTES}])
    client, relay, adapter = make_client(tmp_path)
    response = organize(client)
    assert response.json()["status"] == "completed"
    assert response.json()["points_charged"] == 3.0
    assert NET_NOTES in relay.calls[0]["prompt"]
    assert adapter.calls[0]["parent"] == COURSE_NET


def test_organize_flow_ambiguous_courses_block_with_honest_no_notes_copy(tmp_path: Path):
    seed_course(tmp_path, "net-26", SUFFIXED_HALF,
                [{"title": NOTION_NET_LECTURE, "notes": NET_NOTES}])
    seed_course(tmp_path, "net-25", SUFFIXED_HALF_OLD,
                [{"title": NOTION_NET_LECTURE, "notes": NET_ALT_NOTES}])
    client, relay, adapter = make_client(tmp_path)
    response = organize(client)
    body = response.json()
    assert body["status"] == "blocked"
    assert "所选范围没有可用课堂笔记" in body["reason"]
    assert relay.calls == [] and adapter.calls == []
