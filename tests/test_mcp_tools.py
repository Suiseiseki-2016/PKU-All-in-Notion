"""Tests for the read-only local MCP tool layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pku_sync.mcp_tools import LocalCourseTools


def course_tree(tmp_path: Path) -> LocalCourseTools:
    root = tmp_path / "data"
    course = root / "计算机网络"
    (course / "announcements").mkdir(parents=True)
    (course / "materials" / "第一讲").mkdir(parents=True)
    (course / "recordings" / "2026-09-01_第1-2节" / "keyframes").mkdir(parents=True)
    (root / "logs").mkdir()
    (course / "course.json").write_text(
        json.dumps(
            {
                "course_id": "_1_1",
                "name": "计算机网络",
                "code": "04835010",
                "synced_at": "2026-09-15T06:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        "utf-8",
    )
    (course / "assignments.md").write_text("# 作业\n\n第一题\n", "utf-8")
    (course / "announcements" / "通知.md").write_text("# 通知\n", "utf-8")
    (course / "materials" / "第一讲" / "_index.md").write_text("# 第一讲\n", "utf-8")
    (course / "recordings" / "index.json").write_text(
        json.dumps(
            [
                {
                    "course_id": "_1_1",
                    "title": "2026-09-01第1-2节",
                    "recorded_at": "2026-09-01 08:00:00",
                }
            ],
            ensure_ascii=False,
        ),
        "utf-8",
    )
    rec = course / "recordings" / "2026-09-01_2026-09-01第1-2节"
    rec.mkdir()
    (rec / "notes.md").write_text("# 笔记\n" + "内容" * 20, "utf-8")
    (rec / "keyframes").mkdir()
    (rec / "keyframes" / "a.jpg").write_bytes(b"jpg")
    (root / "logs" / "latest.daily.log").write_text("EXIT_CODE=0\n", "utf-8")
    (root / "logs" / "summary_20260915.md").write_text("# 简报\n无新增\n", "utf-8")
    return LocalCourseTools(root)


def test_health_and_list_courses(tmp_path):
    tools = course_tree(tmp_path)
    assert tools.health() == {
        "ok": True,
        "data_dir": str(tools.data_dir),
        "courses": 1,
    }
    assert tools.list_courses()[0]["name"] == "计算机网络"


def test_list_files_filters_sections(tmp_path):
    tools = course_tree(tmp_path)
    assignments = tools.list_files("计算机网络", "assignments")
    assert [row["path"] for row in assignments] == ["计算机网络/assignments.md"]
    materials = tools.list_files("计算机网络", "materials")
    assert [row["path"] for row in materials] == ["计算机网络/materials/第一讲/_index.md"]
    all_paths = {row["path"] for row in tools.list_files()}
    assert "logs/latest.daily.log" in all_paths
    assert "计算机网络/announcements/通知.md" in all_paths


def test_read_text_limits_and_blocks_escape(tmp_path):
    tools = course_tree(tmp_path)
    value = tools.read_text("计算机网络/assignments.md", max_chars=5)
    assert value["truncated"] is True
    assert len(value["content"]) == 5
    with pytest.raises(ValueError, match="escapes"):
        tools.read_text("../secret.txt")
    with pytest.raises(ValueError, match="relative"):
        tools.read_text(str((tmp_path / "data" / "计算机网络" / "assignments.md").resolve()))
    with pytest.raises(ValueError, match="unsupported"):
        tools.read_text("计算机网络/recordings/2026-09-01_2026-09-01第1-2节/keyframes/a.jpg")


def test_recording_and_daily_status(tmp_path):
    tools = course_tree(tmp_path)
    rows = tools.recording_status()
    assert rows[0]["status"] == "notes_ready"
    assert rows[0]["keyframes"] == 1
    daily = tools.daily_status()
    assert daily["latest_log"]["content"] == "EXIT_CODE=0\n"
    assert "无新增" in daily["latest_summary"]["content"]


def test_unknown_section_and_bad_course_folder(tmp_path):
    tools = course_tree(tmp_path)
    with pytest.raises(ValueError, match="unknown section"):
        tools.list_files(section="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="direct child"):
        tools.list_files("../计算机网络", "all")
