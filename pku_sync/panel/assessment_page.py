"""One page per course for the assessment rules a student must not miss.

A lecture note is a record of what was taught. Grading weights, exam rules, the
submission channel and how many assignments there are belong to the course, not
to a single lecture, and they are scattered across the course handbook, the
teacher's spoken words and the teaching network. This page gathers them in one
place. Each line keeps the source it came from, so a spoken "四六" and a
handbook line about grading stay separate and nobody has to guess which source
said what.
"""
from __future__ import annotations

import re
from pathlib import Path

PAGE_TITLE = "考核评测"
APPENDIX_HEADING = "## 作业与考试口头线索"
INTRO = (
    "本页把考核与作业要求集中列出，按来源分开，不做合并判断。"
    "正式题目、截止时间与提交入口以教学网作业卡片为准；"
    "课程手册与老师原话可能已过期或识别有误，请对照原文件与原录像核对。"
)
_STAMP = re.compile(r"\[(\d{1,3}:\d{2})\]")
_BULLET = re.compile(r"^\s*[-*+]\s+")
_LEAD = re.compile(r"^\*\*[^*]+\*\*")
LEAD = "lead"
RAW = "raw"
_NO_HANDBOOK = "- 未读到可用的手册正文；请打开原文件核对考核与提交要求。"
_NO_ASSIGNMENTS = "- 教学网目录中尚未检出作业卡片或公告待办。"
_NO_SPOKEN = "- 尚未从录像中检出考核或作业相关的原话。"


def spoken_sections(note_text: str) -> tuple[list[str], list[str]]:
    """``(leads, raw)`` from one note's appendix.

    The appendix has two parts and they are not equally useful. ``课业安排速览``
    holds lines the pipeline already classified by kind and certainty, so they
    read as a checklist. ``相关原话`` holds raw ASR excerpts, which carry the
    teacher's exact wording but also every misheard word around it. Keeping the
    two apart is what makes the page readable instead of a transcript dump.
    """
    leads: list[str] = []
    raw: list[str] = []
    inside = False
    section = ""
    for line in str(note_text or "").splitlines():
        if line.startswith("## "):
            inside = line.startswith(APPENDIX_HEADING)
            section = ""
            continue
        if not inside:
            continue
        if line.startswith("### "):
            section = line[4:].strip()
            continue
        if not _STAMP.search(line):
            continue
        # Strip the list bullet only: the line may open with a bold label.
        body = _BULLET.sub("", line).strip()
        if not body:
            continue
        if _LEAD.match(body) or "安排" in section:
            leads.append(body)
        else:
            raw.append(body)
    return leads, raw


def spoken_lines(note_text: str) -> list[str]:
    """Every timed coursework line of one note's appendix, leads first."""
    leads, raw = spoken_sections(note_text)
    return [*leads, *raw]


def collect_spoken_lines(data_dir: Path, course_id: str) -> list[tuple[str, str, str]]:
    """``(date, kind, line)`` for every lecture note of one course."""
    from ..pipeline import collect_jobs

    rows: list[tuple[str, str, str]] = []
    for job in collect_jobs(data_dir, course_id):
        note = job.directory / "notes.md"
        if not note.is_file():
            continue
        try:
            text = note.read_text("utf-8")
        except OSError:
            continue
        date = str(getattr(job.recording, "date", "") or "")
        leads, raw = spoken_sections(text)
        rows.extend((date, LEAD, line) for line in leads)
        rows.extend((date, RAW, line) for line in raw)
    return rows


def _handbook_lines(course: dict) -> list[str]:
    lines: list[str] = []
    for item in course.get("handbook_reviews") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("filename") or item.get("title") or "课程手册")
        status = str(item.get("status") or "")
        lines.append(f"**{name}**" + (f" · {status}" if status else ""))
        for cue in item.get("time_preview") or []:
            lines.append(f"- 时间／提交线索 · PDF 第 {cue.get('page')} 页 · {cue.get('text')}")
        for cue in item.get("presence_preview") or []:
            lines.append(f"- {cue.get('label')} · PDF 第 {cue.get('page')} 页 · {cue.get('text')}")
    return lines


def _assignment_lines(course: dict) -> list[str]:
    lines: list[str] = []
    for item in course.get("assignments") or []:
        if not isinstance(item, dict):
            continue
        due = item.get("due_at_label") or item.get("due_at") or "未公布"
        lines.append(f"- **正式作业卡片** · {item.get('title') or '未命名任务'}"
                     f" · 截止：{due}")
        quote = item.get("teacher_deadline_quote")
        if quote and due == "未公布":
            lines.append(f"  老师原文截止线索：{quote}（系统截止字段为空）")
    for item in course.get("announcement_tasks") or []:
        if not isinstance(item, dict):
            continue
        lines.append("- **公告待办** · " + str(item.get("title") or "未命名待办")
                     + f" · {item.get('due_at_label') or item.get('due_at') or '未公布'}")
        if item.get("instructions"):
            lines.append(f"  原公告：{item['instructions']}")
    return lines


def _spoken_lines(spoken: list[tuple[str, str, str]] | None) -> list[str]:
    """Leads first, raw excerpts under a heading of their own.

    The classified leads read as a checklist; the raw excerpts carry the
    teacher's exact wording but also every misheard word, so they stay below
    and labelled. Mixing them made the page read as a transcript dump.
    """
    rows = list(spoken or [])
    lines: list[str] = []
    for date, kind, line in rows:
        if kind != LEAD:
            continue
        lines.append(f"- {f'[{date}] ' if date else ''}{line}")
    raw = [row for row in rows if row[1] == RAW]
    if raw:
        lines += ["", "**转写原话（可能含错词，仅供核对）**", ""]
        for date, _kind, line in raw:
            lines.append(f"- {f'[{date}] ' if date else ''}{line}")
    return lines


def assessment_markdown(course: dict,
                        spoken: list[tuple[str, str, str]] | None = None) -> str:
    """The page body, grouped by source, with an empty source stated plainly.

    Sources are never merged into one statement: the handbook's own line, the
    formal card and the teacher's words each keep their provenance, because only
    the teaching network card is authoritative.
    """
    blocks: list[str] = [INTRO, "", "## 课程手册（考核与提交要求）", ""]
    blocks += _handbook_lines(course) or [_NO_HANDBOOK]
    blocks += ["", "## 教学网正式作业与公告待办", ""]
    blocks += _assignment_lines(course) or [_NO_ASSIGNMENTS]
    blocks += ["", "## 老师原话（按录像时间，自动转写待核对）", ""]
    blocks += _spoken_lines(spoken) or [_NO_SPOKEN]
    return "\n".join(blocks) + "\n"
