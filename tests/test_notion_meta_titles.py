"""Title normalization, semester scoping, and lecture-title parsing (VAL-META-001/002/003)."""

from __future__ import annotations

import datetime

import pytest

from pku_sync.notion_meta import titles


def test_strip_icon_and_normalize():
    assert titles.strip_icon("🌐 计算机网络") == "计算机网络"
    assert titles.strip_icon("📚 2026秋季学期学习中心") == "2026秋季学期学习中心"
    assert titles.normalize_title("🌐 计算机网络") == "计算机网络"
    # whitespace variants (ASCII + full-width U+3000) collapse for exact matching
    assert titles.normalize_title(" 计算机网络\u3000") == "计算机网络"
    assert titles.normalize_title("认知心理学  实验\u3000手册") == "认知心理学 实验 手册"
    # CJK brackets are punctuation, not icons — kept
    assert titles.normalize_title("《课程总结》") == "《课程总结》"


def test_current_semester_label():
    assert titles.current_semester_label(datetime.datetime(2026, 9, 19)) == "2026 下半学期"
    assert titles.current_semester_label(datetime.datetime(2026, 3, 1)) == "2026 上半学期"


def test_is_hub_family_and_matches_semester():
    assert titles.is_hub_family("Class Notes 2026 下半学期")
    # the verified typo variant is the same family
    assert titles.is_hub_family("Clash Notes 2026上半学期")
    assert not titles.is_hub_family("🌐 计算机网络")
    assert not titles.is_hub_family("Blackboard 作业提交自动化经验（2026-09-12）")
    # semester matching is space-insensitive containment
    assert titles.matches_semester("Class Notes 2026 下半学期", "2026 下半学期")
    assert not titles.matches_semester("Clash Notes 2026上半学期", "2026 下半学期")
    assert titles.matches_semester("Class Notes 2026下半学期", "2026 下半学期")


def test_special_and_noise_child_patterns():
    assert titles.is_learning_center("📚 2026秋季学期学习中心")
    assert titles.is_learning_center("2026秋季学期学习中心")
    assert not titles.is_learning_center("计算机网络")
    assert titles.is_recording_notes_hub("课堂录像笔记")
    assert not titles.is_recording_notes_hub("计算机网络")
    # verified real noise: dated-suffix hub child is a log page, not a course
    assert titles.is_noise_child("Blackboard 作业提交自动化经验（2026-09-12）")
    assert not titles.is_noise_child("🌐 计算机网络")
    assert not titles.is_noise_child("📚 2026秋季学期学习中心")


@pytest.mark.parametrize(
    "raw,number,date,period",
    [
        # the three verified real title variants
        ("第一讲 · 计算机网络", 1, "", ""),
        ("第二讲 · 计算机网络（2026-09-09 第1-2节）", 2, "2026-09-09", "第1-2节"),
        ("《第三讲 · 计算机网络（2026-09-14 第5-6节）》", 3, "2026-09-14", "第5-6节"),
        # icons exist on real lecture pages despite the writer spec
        ("🌐 第一讲 · 计算机网络", 1, "", ""),
        ("🌐 第二讲 · 计算机网络（2026-09-16 第1-2节）", 2, "2026-09-16", "第1-2节"),
        # digits and compound Chinese numerals
        ("第10讲 · 数据结构", 10, "", ""),
        ("第二十四讲 · 计算机网络（2026-12-30 第3-4节）", 24, "2026-12-30", "第3-4节"),
        # date without 节次 and 节次 without date
        ("第五讲 · 计算机网络（2026-09-30）", 5, "2026-09-30", ""),
        ("第六讲 · 计算机网络（第7-8节）", 6, "", "第7-8节"),
    ],
)
def test_parse_lecture_title_verified_variants(raw, number, date, period):
    parsed = titles.parse_lecture_title(raw)
    assert parsed is not None
    assert (parsed.number, parsed.date, parsed.period) == (number, date, period)


@pytest.mark.parametrize(
    "raw",
    [
        "《课程总结》",
        "复习资料",
        "第二周 · 计算机网络",
        "第1-2节录像.mp4",
        "",
    ],
)
def test_parse_lecture_title_rejects_non_lectures(raw):
    assert titles.parse_lecture_title(raw) is None
