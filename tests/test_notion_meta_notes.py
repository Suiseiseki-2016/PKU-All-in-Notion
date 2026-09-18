"""课堂录像笔记 line parsing (VAL-META-006): both verified forms, no body capture."""

from __future__ import annotations

import dataclasses
import json

import notion_meta_fake as fake

from pku_sync.notion_meta import notes


def test_parse_url_line_form():
    blocks = [
        fake.paragraph(
            f"《第一讲 · 计算机网络（2026-09-07 第5-6节）》→ {fake.notion_url(fake.LECTURE1)}"
        )
    ]
    lines, skipped = notes.parse_note_lines(blocks)
    assert skipped == 0
    assert len(lines) == 1
    line = lines[0]
    assert line.target_id == fake.LECTURE1
    assert line.title == "第一讲 · 计算机网络（2026-09-07 第5-6节）"
    assert (line.number, line.date, line.period) == (1, "2026-09-07", "第5-6节")


def test_parse_mention_line_form():
    blocks = [
        fake.paragraph(
            [
                fake.text_piece("《第二讲 · 计算机网络（2026-09-09 第1-2节）》→ "),
                fake.page_mention_piece(
                    "第二讲 · 计算机网络（2026-09-09 第1-2节）", fake.LECTURE2
                ),
            ]
        )
    ]
    lines, skipped = notes.parse_note_lines(blocks)
    assert skipped == 0
    assert len(lines) == 1
    assert lines[0].target_id == fake.LECTURE2
    assert (lines[0].number, lines[0].date, lines[0].period) == (2, "2026-09-09", "第1-2节")


def test_parse_markdown_link_line_form():
    blocks = [
        fake.paragraph(
            f"《第三讲 · 计算机网络（2026-09-14 第5-6节）》→ "
            f"[第三讲 · 计算机网络]({fake.notion_url(fake.LECTURE3)})"
        )
    ]
    lines, skipped = notes.parse_note_lines(blocks)
    assert skipped == 0
    assert len(lines) == 1
    assert lines[0].target_id == fake.LECTURE3


def test_line_without_resolvable_target_is_skipped():
    blocks = [fake.paragraph("《第四讲 · 计算机网络（2026-09-21 第3-4节）》→ 待补链接")]
    lines, skipped = notes.parse_note_lines(blocks)
    assert lines == []
    assert skipped == 1


def test_transcript_preview_body_never_captured():
    """Week-1 subpages embed transcript previews — only link lines survive."""
    blocks = [
        # non-text blocks are ignored entirely
        {"object": "block", "type": "divider", "divider": {}},
        # transcript-preview paragraph with signed S3 URL and local path honeypots
        fake.paragraph(f"{fake.TRANSCRIPT_PREVIEW} {fake.SIGNED_S3_URL} {fake.LOCAL_PATH}"),
        # the real link line
        fake.paragraph(
            f"《第一讲 · 计算机网络（2026-09-07 第5-6节）》→ {fake.notion_url(fake.LECTURE1)}"
        ),
    ]
    lines, skipped = notes.parse_note_lines(blocks)
    assert skipped == 0
    assert len(lines) == 1
    dump = json.dumps([dataclasses.asdict(line) for line in lines], ensure_ascii=False)
    for needle in (fake.TRANSCRIPT_PREVIEW, fake.SIGNED_S3_URL, fake.LOCAL_PATH):
        assert needle not in dump
