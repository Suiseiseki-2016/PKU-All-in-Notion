"""Course-summary snapshots built from material the app already has.

A lecture note records one lecture. Before an exam a student needs the whole
course in one page: which lectures exist, which ones already have a note,
what the notes say the teacher called out, and which formal work is due. Every
line here comes from a local lecture page or the campus catalog snapshot, so
the page never invents a fact. Snapshots accumulate by date and an existing
page is never rewritten.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ..notion import NotionClient, markdown_to_blocks
from ..pipeline import collect_jobs
from .campus_catalog import campus_course, campus_courses
from .recordings_api import recording_id

logger = logging.getLogger(__name__)

SUMMARY_SUFFIX = "课程总结"
_MAX_TOPICS = 14
_MAX_POINTS = 3
_MAX_POINT_CHARS = 220
_MAX_UNCERTAIN = 12
_MAX_CLUES = 12


def beijing_today(now: datetime | None = None) -> date:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("课程总结需要带时区的当前时间。")
    return current.astimezone(timezone(timedelta(hours=8))).date()


def summary_title(course_title: str, day: date) -> str:
    return f"{course_title}｜{SUMMARY_SUFFIX}（截至 {day.isoformat()}）"


def _plain(value: str) -> str:
    text = re.sub(r"\*\*|`", "", value).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > _MAX_POINT_CHARS:
        text = text[:_MAX_POINT_CHARS] + "…"
    return text


_TIMESTAMP_BULLET = re.compile(r"^[*\-+]\s*\[(\d{1,2}:\d{2})\]\s*(.+)$")
_HEADING = re.compile(r"^(#{2,4})\s+(.+)$")
_BULLET = re.compile(r"^[*\-+]\s+(.+)$")
_BOLD_LINE = re.compile(r"^\*\*([^*]+)\*\*$")


def note_outline(markdown: str) -> dict:
    """Pull topics, spoken homework clues and open questions out of one note.

    Notes indent sub-bullets under a leading point. Those children belong to
    the point above them, so they are folded in rather than flattened into
    unrelated siblings.
    """
    topics: list[tuple[str, list[tuple[str, list[str]]]]] = []
    clues: list[str] = []
    uncertain: list[str] = []
    section = ""
    current: tuple[str, list[tuple[str, list[str]]]] | None = None
    last_point: tuple[str, list[str]] | None = None
    last_point_indent = -1
    for raw in markdown.replace("\r\n", "\n").split("\n"):
        stripped = raw.strip()
        if not stripped or stripped.startswith(">"):
            continue
        heading = _HEADING.match(stripped)
        if heading:
            if heading.group(1) == "##":
                section = heading.group(2).strip()
                current = last_point = None
                continue
            current = (heading.group(2).strip(), [])
            topics.append(current)
            last_point = None
            continue
        stamped = _TIMESTAMP_BULLET.match(stripped)
        if stamped:
            uncertain.append(f"[{stamped.group(1)}] " + _plain(stamped.group(2)))
            continue
        bullet = _BULLET.match(stripped)
        if section.startswith("作业与考试口头线索"):
            if bullet:
                clues.append(_plain(bullet.group(1)))
            continue
        bold = _BOLD_LINE.match(stripped)
        if bold:
            name = _plain(bold.group(1))
            # A sub-heading that opens a topic keeps its parent's label, so the
            # digest reads as a hierarchy instead of an orphaned heading.
            if current is not None and not current[1] and current[0]:
                topics.pop()
                name = f"{current[0]} › {name}"
            current = (name, [])
            topics.append(current)
            last_point = None
            continue
        if bullet and current is not None:
            value = _plain(bullet.group(1))
            indent = len(raw) - len(raw.lstrip(" \t"))
            # Every sibling at the deeper indent belongs to the point above,
            # so the comparison is against that point's own indent.
            if last_point is not None and indent > last_point_indent:
                last_point[1].append(value)
            elif value:
                last_point = (value, [])
                last_point_indent = indent
                current[1].append(last_point)
    return {"topics": topics, "clues": clues, "uncertain": uncertain}


def _point_text(point: str, children: list[str]) -> str:
    if not children:
        return point
    joiner = "" if point.endswith(("：", ":")) else "；"
    return point + joiner + "；".join(children)


def _lecture_rows(data_dir: Path, course: dict) -> list[dict]:
    jobs = {recording_id(job): job for job in collect_jobs(data_dir, course["id"])}
    rows = []
    for lesson in course["lessons"]:
        job = jobs.get(lesson["id"])
        note_path = job.directory / "notes.md" if job is not None else None
        markdown = ""
        if note_path is not None and note_path.is_file():
            try:
                markdown = note_path.read_text("utf-8")
            except OSError:
                markdown = ""
        rows.append({
            "id": lesson["id"],
            "title": lesson["title"],
            "date": lesson["date"],
            "has_note": bool(markdown.strip()),
            "notion_note_url": lesson.get("notion_note_url") or "",
            "notion_lecture_url": lesson.get("notion_lecture_url") or "",
            "outline": note_outline(markdown) if markdown.strip() else {"topics": [], "clues": [], "uncertain": []},
        })
    return rows


def _label(row: dict) -> str:
    base = " · ".join(part for part in (row["date"], row["title"]) if part)
    return f"《{base or row['id']}》"


def _lecture_link(row: dict) -> str:
    return row["notion_note_url"] or row["notion_lecture_url"]


def _assignment_entries(course: dict) -> list[tuple[str, str | None]]:
    entries: list[tuple[str, str | None]] = []
    for item in course.get("assignments") or []:
        if not isinstance(item, dict):
            continue
        due = item.get("due_at_label") or ""
        if not due and item.get("teacher_deadline_quote"):
            due = "老师原文写 " + str(item["teacher_deadline_quote"]) + "（系统无独立截止字段）"
        entries.append((f"正式作业 · {item.get('title') or '未命名作业'} · 截止 {due or '未公布'}", None))
    for item in course.get("announcement_tasks") or []:
        if not isinstance(item, dict):
            continue
        due = item.get("due_at_label") or item.get("due_at") or "未公布"
        entries.append((f"公告待办 · {item.get('notice_title') or item.get('title') or '未命名待办'} · 截止 {due}", None))
    for item in course.get("handbook_reviews") or []:
        if not isinstance(item, dict):
            continue
        entries.append((f"课程手册待核对 · {item.get('filename') or item.get('title') or '未命名手册'}"
                        "（需向老师或助教核对，不是正式作业卡片）", None))
    status = course.get("assignments_status")
    if status == "not_synced":
        entries.append(("教学网正式作业目录尚未同步；不能据此判断没有作业。", None))
    elif status in {"partial", "error"}:
        entries.append(("教学网作业来源读取不完整；以上清单可能遗漏，请到教学网核对。", None))
    return entries


def summary_entries(data_dir: Path, course_id: str, *, day: date | None = None) -> list[tuple[str, str | None]]:
    """The summary page as ordered (line, link) entries.

    Text lines render as Notion blocks; entries carrying a link become real
    clickable paragraphs, so a lecture page is one tap away.
    """
    course = campus_course(data_dir, course_id)
    stamp = (day or beijing_today()).isoformat()
    rows = _lecture_rows(data_dir, course)
    noteless = [row for row in rows if not row["has_note"]]
    lecture_links = [row for row in rows if _lecture_link(row)]

    entries: list[tuple[str, str | None]] = [
        (f"# {course['title']}｜{SUMMARY_SUFFIX}（截至 {stamp}）", None),
        ("本页由 PKU All in Notion 根据本机讲次页与教学网目录快照自动汇总；不新造知识点，"
         "完整内容以各讲次页和教学网原页面为准。", None),
        ("## 课程定位与覆盖", None),
        (f"- 课程：{course['title']}（课程号 {course['code'] or '未提供'}；"
         f"{course['term'] or '学期未记录'}）", None),
        (f"- 讲次：本机收录 {len(rows)} 节；已有笔记 {len(rows) - len(noteless)} 节；"
         f"待整理 {len(noteless)} 节", None),
        (f"- 教学网目录读取：{course.get('synced_at_label') or '未记录'}", None),
    ]

    entries.append(("## 讲次与笔记状态", None))
    if not rows:
        entries.append(("- 本课程暂未收录任何讲次录像。", None))
    for row in rows:
        link = _lecture_link(row)
        status = "已有笔记" if row["has_note"] else "待整理"
        line = f"{_label(row)} · {status}"
        entries.append((line, link or None))

    entries.append(("## 主题要点（逐节摘自已发布笔记）", None))
    if not any(row["has_note"] for row in rows):
        entries.append(("- 本课程还没有已发布笔记，无法汇总主题要点；请先在课程页整理讲次。", None))
    for row in rows:
        if not row["has_note"]:
            continue
        outline = row["outline"]
        topics = outline["topics"][:_MAX_TOPICS]
        if not topics:
            entries.append((f"### {_label(row)}", None))
            entries.append(("- 这节笔记没有分主题标题；请打开讲次页查看完整内容。", None))
            continue
        entries.append((f"### {_label(row)}", None))
        for name, points in topics:
            rendered = [_point_text(point, children) for point, children in points[:_MAX_POINTS]]
            detail = "；".join(rendered) if rendered else "（这节笔记未在此主题下写要点）"
            entries.append((f"- {name}：{detail}", None))

    entries.append(("## 老师口头线索（笔记原文摘录，待核对）", None))
    clue_rows = [(row, clue) for row in rows for clue in row["outline"]["clues"]]
    if not clue_rows:
        entries.append(("- 已有笔记中没有检出明确的口头作业或考试线索；这不代表老师没有布置任务。", None))
    for row, clue in clue_rows[:_MAX_CLUES]:
        entries.append((f"- {_label(row)} · {clue}", None))

    entries.append(("## 课程考核与学习要求", None))
    requirements = _assignment_entries(course)
    if not requirements:
        entries.append(("- 教学网目录中暂未检出正式作业、公告待办或待核对手册。", None))
    entries.extend(requirements)

    entries.append(("## 待确认事项", None))
    open_items = [(row, item) for row in rows for item in row["outline"]["uncertain"]]
    if not open_items:
        entries.append(("- 已发布笔记中没有登记待确认条目。", None))
    for row, item in open_items[:_MAX_UNCERTAIN]:
        entries.append((f"- {_label(row)} {item}", None))
    if noteless:
        entries.append((f"- 待整理讲次 {len(noteless)} 节，其内容尚未进入本页："
                        + "、".join(_label(row) for row in noteless[:8])
                        + ("…" if len(noteless) > 8 else ""), None))

    entries.append(("## 资料依据与缺口", None))
    entries.append((f"- 覆盖讲次 {len(rows) - len(noteless)} / {len(rows)} 节；"
                    f"生成时间 {datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')} 北京时间。", None))
    if lecture_links:
        for row in lecture_links:
            entries.append((f"打开讲次页：{_label(row)}", _lecture_link(row)))
    else:
        entries.append(("- 还没有写入 Notion 的讲次页；请先在课程页建立 Notion 讲次。", None))
    if noteless:
        entries.append(("- 缺口：以下讲次尚无笔记，本页未汇总其内容 —— "
                        + "、".join(_label(row) for row in noteless[:8])
                        + ("…" if len(noteless) > 8 else ""), None))
    return entries


def summary_markdown(data_dir: Path, course_id: str, *, day: date | None = None) -> str:
    """Plain-text rendering of the same snapshot, for tests and previews."""
    return "\n".join(line for line, _ in summary_entries(data_dir, course_id, day=day))


def _linked_paragraph(label: str, url: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": label, "link": {"url": url}}}
    ]}}


def summary_blocks(data_dir: Path, course_id: str, *, day: date | None = None) -> list[dict]:
    blocks: list[dict] = []
    pending: list[str] = []
    for line, url in summary_entries(data_dir, course_id, day=day):
        if url:
            if pending:
                blocks += markdown_to_blocks("\n".join(pending))
                pending = []
            blocks.append(_linked_paragraph(line, url))
        else:
            pending.append(line)
    if pending:
        blocks += markdown_to_blocks("\n".join(pending))
    return blocks


def find_course_page(client: NotionClient, data_dir: Path, course_id: str) -> dict:
    from .notion_home import _course_titles, saved_home

    home = saved_home(data_dir)
    if not home:
        raise ValueError("请先选择 Notion 父页面并创建学习主页。")
    title = _course_titles(data_dir)[course_id]
    matches = [page for page in client.list_child_pages(home["id"]) if page.get("title") == title]
    if not matches:
        raise ValueError("这门课还没有 Notion 课程页；请先同步 Notion 课程目录。")
    return matches[0]


def _course_folder(data_dir: Path, course_id: str) -> Path | None:
    from .campus_catalog import _courses

    return next((folder for folder, key, _title, _meta in _courses(data_dir) if key == course_id),
                None)


def _save_summary_marker(data_dir: Path, course_id: str, result: dict) -> None:
    """Remember the newest snapshot so the course page can link straight to it."""
    folder = _course_folder(data_dir, course_id)
    if folder is None:
        return
    marker = folder / "summary.json"
    payload = {"title": result["title"], "url": result["url"],
               "created": result["created"],
               "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        temporary = marker.with_name(marker.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
        temporary.replace(marker)
    except OSError:
        logger.warning("course summary marker could not be saved for %s", course_id)


def publish_course_summary(client: NotionClient, data_dir: Path, course_id: str, *,
                           day: date | None = None) -> dict:
    """Create today's snapshot under the course page, or reuse the same day's."""
    stamp = day or beijing_today()
    course = campus_course(data_dir, course_id)
    title = summary_title(course["title"], stamp)
    course_page = find_course_page(client, data_dir, course_id)
    children = client.list_child_pages(course_page["id"])
    existing = [page for page in children if (page.get("title") or "") == title]
    if existing:
        page_id = existing[0]["id"]
        result = {"course_id": course_id, "title": title, "created": False,
                  "url": existing[0].get("url") or f"https://www.notion.so/{page_id.replace('-', '')}"}
        _save_summary_marker(data_dir, course_id, result)
        return result
    # Snapshots accumulate by date: an older day's page keeps its content and
    # today's run adds today's page rather than rewriting it.
    page = client.create_page(course_page["id"], title,
                              children=summary_blocks(data_dir, course_id, day=stamp),
                              retry=False)
    page_id = page["id"]
    result = {"course_id": course_id, "title": title, "created": True,
              "url": page.get("url") or f"https://www.notion.so/{page_id.replace('-', '')}"}
    _save_summary_marker(data_dir, course_id, result)
    return result


def publish_course_summaries(data_dir: Path, token: str, course_ids: list[str], *,
                             day: date | None = None,
                             on_progress: Callable[[int, int, str], None] | None = None,
                             on_course: Callable[[str], None] | None = None) -> dict:
    """Snapshot every requested course; one failing course never stops the rest."""
    created = existing = 0
    urls: list[str] = []
    failures: list[tuple[str, str]] = []
    titles: dict[str, str] = {}
    total = len(course_ids)
    with NotionClient(token) as client:
        for index, course_id in enumerate(course_ids, 1):
            try:
                title = campus_course(data_dir, course_id)["title"]
            except ValueError:
                title = course_id
            titles[course_id] = title
            if on_course:
                on_course(title)
            if on_progress:
                on_progress(index - 1, total, title)
            try:
                result = publish_course_summary(client, data_dir, course_id, day=day)
            except Exception as exc:  # one bad course must not stop the batch
                failures.append((title, str(exc)))
            else:
                created += int(result["created"])
                existing += int(not result["created"])
                urls.append(result["url"])
            if on_progress:
                on_progress(index, total, title)
    return {"requested": total, "created": created, "existing": existing,
            "urls": urls, "failures": failures}
