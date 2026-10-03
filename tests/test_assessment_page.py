"""The course-level 考核评测 page: rules gathered, sources kept apart.

A lecture note records the lecture. Grading weights, exam rules, the submission
channel and the number of assignments are course-level, so they get their own
page rather than being rewritten into the note.
"""

import pytest

from pku_sync.notion import markdown_to_blocks
from pku_sync.panel import assessment_page, notion_home
from pku_sync.panel.assessment_page import (LEAD, RAW, assessment_markdown,
                                            spoken_lines, spoken_sections)


class FakeNotion:
    def __init__(self):
        self.pages: dict[str, list[dict]] = {}
        self.blocks: dict[str, list[dict]] = {}
        self.created: list[tuple[str, str]] = []
        self.replaced: list[str] = []
        self._next = 0

    def list_child_pages(self, parent_id):
        return self.pages.get(parent_id, [])

    def create_page(self, parent_id, title, children=None, retry=True):
        self._next += 1
        page = {"id": f"page-{self._next}", "title": title}
        self.pages.setdefault(parent_id, []).append(page)
        self.created.append((parent_id, title))
        return page

    def list_children(self, page_id):
        return self.blocks.get(page_id, [])

    def replace_page_content(self, page_id, blocks):
        self.replaced.append(page_id)
        self.blocks[page_id] = blocks

    def append_blocks(self, page_id, blocks, retry=True):
        self.blocks.setdefault(page_id, []).extend(blocks)


def _course() -> dict:
    return {
        "handbook_reviews": [{
            "filename": "课程手册.pdf", "status": "已读取手册正文",
            "time_preview": [{"page": 4, "text": "期末成绩占 60%，平时成绩占 40%"}],
            "presence_preview": [{"label": "随堂考勤", "page": 5,
                                  "text": "缺勤三次以上不得参加考试"}],
        }],
        "assignments": [{"title": "第一次作业", "due_at_label": "2026-10-01 23:59",
                         "source": "content-tree"}],
        "announcement_tasks": [{"title": "实验报告 · 10月8日",
                                "due_at_label": "10月8日（老师公告原文）",
                                "instructions": "10月8日前提交实验报告"}],
    }


def test_assessment_markdown_keeps_every_source_and_its_provenance():
    spoken = [("2026-09-17", LEAD, "**提交位置（已明确说出，待核对）** [60:18] 就在教学网交"),
              ("2026-09-17", RAW, "[58:41] 平时作业部分加上那个期末比试吧 就是四六吧")]
    text = assessment_markdown(_course(), spoken)
    assert "## 课程手册（考核与提交要求）" in text
    assert "## 教学网正式作业与公告待办" in text
    assert "## 老师原话（按录像时间，自动转写待核对）" in text
    # Every source keeps its own line; nothing is merged into one statement.
    assert "期末成绩占 60%，平时成绩占 40%" in text
    assert "缺勤三次以上不得参加考试" in text
    assert "第一次作业" in text
    assert "2026-10-01 23:59" in text
    assert "10月8日前提交实验报告" in text
    assert "四六" in text and "[2026-09-17]" in text
    # Only the teaching-network card is authoritative.
    assert "以教学网作业卡片为准" in text


def test_classified_leads_are_listed_above_the_raw_excerpts():
    spoken = [("2026-09-17", RAW, "[58:41] 好 咱们是怎么考试 就是四六吧"),
              ("2026-09-17", LEAD, "**提交位置（已明确说出，待核对）** [60:18] 就在教学网交")]
    text = assessment_markdown({}, spoken)
    assert "**提交位置（已明确说出，待核对）**" in text
    # The raw excerpts stay below, under their own label: mixing them made the
    # page read as a transcript dump.
    assert "**转写原话（可能含错词，仅供核对）**" in text
    assert text.index("提交位置") < text.index("转写原话") < text.index("怎么考试")


def test_assessment_markdown_states_an_empty_source_plainly():
    text = assessment_markdown({}, [])
    assert "未读到可用的手册正文" in text
    assert "尚未检出作业卡片或公告待办" in text
    assert "尚未从录像中检出" in text


def test_spoken_lines_reads_only_the_timed_appendix():
    note = (
        "# 课程\n\n## 课堂内容\n\n### 凯撒密码\n\n明文后移 3 位得到密文。（回看 12:00）\n\n"
        "## 作业与考试口头线索（自动转写原文，待核对）\n\n"
        "### 课业安排速览\n\n"
        "- **提交位置（已明确说出，待核对）** [60:18] 就是书面作业就在教学网交\n\n"
        "### 相关原话\n\n- [58:41] 平时作业部分加上那个期末比试吧\n\n"
        "## 课件读取说明\n\n- [99:99] 这一段不属于考核线索\n"
    )
    assert spoken_lines(note) == [
        "**提交位置（已明确说出，待核对）** [60:18] 就是书面作业就在教学网交",
        "[58:41] 平时作业部分加上那个期末比试吧",
    ]
    leads, raw = spoken_sections(note)
    assert leads == ["**提交位置（已明确说出，待核对）** [60:18] 就是书面作业就在教学网交"]
    assert raw == ["[58:41] 平时作业部分加上那个期末比试吧"]


def test_spoken_lines_is_empty_without_an_appendix():
    assert spoken_lines("# 课程\n\n## 课堂内容\n\n- [12:00] 只讲了一个概念\n") == []


def test_assessment_page_is_created_and_rebuilt_from_its_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(assessment_page, "collect_spoken_lines",
                        lambda *_args: [("2026-09-17", LEAD, "[58:41] 就是四六吧")])
    client = FakeNotion()
    state: dict = {}
    notion_home._sync_assessment_page(client, tmp_path, "course-1", "course-page",
                                      _course(), state)
    assert [title for _parent, title in client.created] == [notion_home.ASSESSMENT_TITLE]
    page_id = client.created[0][1]
    page = client.pages["course-page"][0]
    first = client.blocks[page["id"]][0]
    body = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                   for part in first[first["type"]]["rich_text"])
    assert body.startswith("本页把考核与作业要求集中列出")
    assert state["course-1|考核评测"]

    # A second run with unchanged sources writes nothing.
    before = list(client.blocks[page["id"]])
    notion_home._sync_assessment_page(client, tmp_path, "course-1", "course-page",
                                      _course(), state)
    assert client.blocks[page["id"]] == before
    assert client.replaced == []


def test_assessment_page_refuses_to_overwrite_a_hand_edited_page(tmp_path, monkeypatch):
    monkeypatch.setattr(assessment_page, "collect_spoken_lines", lambda *_args: [])
    client = FakeNotion()
    page = client.create_page("course-page", notion_home.ASSESSMENT_TITLE)
    client.blocks[page["id"]] = markdown_to_blocks("这是我自己写的考核笔记。")
    with pytest.raises(ValueError, match="手动内容"):
        notion_home._sync_assessment_page(client, tmp_path, "course-1", "course-page",
                                          _course(), {})
