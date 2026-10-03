"""Create the student's Notion learning tree only after an explicit choice."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ..notion import NotionClient, markdown_to_blocks
from .campus_catalog import campus_course, campus_courses, display_campus_time
from .recordings_api import _safe_attachment_path

HOME_TITLE = "PKU All in Notion · 学习主页"
_PRESENCE_REQUIRED = re.compile(r"随堂考勤|到课考勤|现场签到|课堂展示|课堂汇报|课堂点评")


def _title(page: dict) -> str:
    properties = page.get("properties") or {}
    for property_value in properties.values():
        if isinstance(property_value, dict) and property_value.get("type") == "title":
            return "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                           for part in property_value.get("title") or [])
    return "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                   for part in page.get("title") or [])


def available_parents(token: str) -> list[dict]:
    with NotionClient(token) as client:
        pages = client.search("", object_type="page")
    return [{"id": page["id"], "title": _title(page) or "未命名页面", "url": page.get("url", "")}
            for page in pages if page.get("id") and not page.get("archived")]


def home_path(data_dir: Path) -> Path:
    return data_dir / "notion-learning-home.json"


def saved_home(data_dir: Path) -> dict:
    try:
        home = json.loads(home_path(data_dir).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return home if isinstance(home, dict) and home.get("id") and home.get("parent_id") else {}


def _save_home(data_dir: Path, value: dict) -> None:
    path = home_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), "utf-8")
    temporary.replace(path)


def _child(client: NotionClient, parent_id: str, title: str) -> dict:
    matches = [page for page in client.list_child_pages(parent_id) if page["title"] == title]
    if len(matches) > 1:
        raise ValueError(f"Notion 中有多个同名页面「{title}」，请先手动确认。")
    if matches:
        page_id = matches[0]["id"]
        page = client.get_page(page_id)
        return {"id": page_id, "url": page.get("url", "")}
    page = client.create_page(parent_id, title, retry=False)
    return {"id": page["id"], "url": page.get("url", "")}


def adopt_oauth_template(data_dir: Path, token: str, page_id: str) -> dict:
    """Use the page Notion duplicated and shared during public OAuth."""
    existing = saved_home(data_dir)
    if existing:
        return existing
    with NotionClient(token) as client:
        page = client.get_page(page_id)
    if not page.get("id"):
        raise ValueError("Notion 没有返回已授权的学习主页。")
    home = {"id": page["id"], "url": page.get("url", ""),
            "parent_id": "oauth-template", "origin": "oauth-template"}
    _save_home(data_dir, home)
    return home
def create_home(data_dir: Path, token: str, parent_id: str) -> dict:
    parents = available_parents(token)
    if parent_id not in {page["id"] for page in parents}:
        raise ValueError("请选择已授权的 Notion 父页面。")
    existing = saved_home(data_dir)
    if existing and existing["parent_id"] != parent_id:
        raise ValueError("学习主页已创建；请先在现有主页继续整理。")
    with NotionClient(token) as client:
        if existing:
            page = client.get_page(existing["id"])
            return {"id": existing["id"], "url": page.get("url", existing.get("url", "")),
                    "parent_id": parent_id}
        page = _child(client, parent_id, HOME_TITLE)
    home = {"id": page["id"], "url": page["url"], "parent_id": parent_id}
    _save_home(data_dir, home)
    return home


def move_home(data_dir: Path, token: str, parent_id: str) -> dict:
    """Move the existing learning tree, preserving its page ID and contents."""
    existing = saved_home(data_dir)
    if not existing:
        raise ValueError("请先创建 Notion 学习主页。")
    if parent_id == existing["id"]:
        raise ValueError("学习主页不能作为自己的父页面。")
    if parent_id not in {page["id"] for page in available_parents(token)}:
        raise ValueError("请选择已授权的 Notion 父页面。")
    with NotionClient(token) as client:
        page = client.get_page(existing["id"])
        current_parent = (page.get("parent") or {}).get("page_id")
        if current_parent != parent_id:
            moved = client.move_page(existing["id"], parent_id)
        else:
            moved = page
    home = {**existing, "url": moved.get("url") or page.get("url") or existing.get("url", ""),
            "parent_id": parent_id}
    _save_home(data_dir, home)
    return home


def _course_titles(data_dir: Path) -> dict[str, str]:
    rows = campus_courses(data_dir)["courses"]
    counts = Counter(row["title"] for row in rows)
    return {row["id"]: (row["title"] if counts[row["title"]] == 1
                        else f"{row['title']} · {row['id']}") for row in rows}


def _lecture_titles(course: dict) -> dict[str, str]:
    bases = {lesson["id"]: " · ".join(
        part for part in (lesson["date"], lesson["title"]) if part
    ) for lesson in course["lessons"]}
    counts = Counter(bases.values())
    return {key: (title if counts[title] == 1 else f"{title} · {key[:8]}")
            for key, title in bases.items()}


def _lecture_status_title(base: str, has_note: bool) -> str:
    # A published child page proves that notes exist, not that the full
    # lecture passed evidence and editorial acceptance.
    return ("笔记待验收 · " if has_note else "待整理 · ") + base


def _has_note_child(children: list[dict]) -> bool:
    return any(child.get("title", "").startswith(("转写笔记 ·", "更新笔记 ·"))
               for child in children)


def _find_lecture(children: dict[str, dict], base: str) -> dict | None:
    # A prior sync may have left an empty directory page alongside a lecture
    # containing a published note. Never pick the empty page first.
    for title in (_lecture_status_title(base, True),
                  "旧版笔记待验收 · " + base, "已整理 · " + base,
                  base, _lecture_status_title(base, False)):
        if title in children:
            return children[title]
    return None


def _ensure_status_lecture(client: NotionClient, parent_id: str, base: str,
                           children: dict[str, dict]) -> tuple[dict, bool]:
    page = _find_lecture(children, base)
    made = False
    if page is None:
        title = _lecture_status_title(base, False)
        page, made = _ensure_indexed_child(client, parent_id, title, children)
    notes = client.list_child_pages(page["id"])
    has_note = _has_note_child(notes)
    wanted = _lecture_status_title(base, has_note)
    old_title = page["title"]
    if old_title != wanted:
        blocks = client.list_children(page["id"])
        has_marker = any(
            block.get("type") == "paragraph" and
            "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                    for part in (block.get("paragraph") or {}).get("rich_text") or [])
            == LECTURE_PLACEHOLDER for block in blocks
        )
        if has_marker or has_note:
            client.update_page_properties(
                page["id"], {"title": {"title": [{"text": {"content": wanted}}]}}
            )
            children.pop(old_title, None)
            page["title"] = wanted
            children[wanted] = page
    if has_note:
        empty_title = _lecture_status_title(base, False)
        duplicate = children.get(empty_title)
        if duplicate and duplicate["id"] != page["id"]:
            duplicate_children = client.list_child_pages(duplicate["id"])
            duplicate_blocks = client.list_children(duplicate["id"])
            if (not duplicate_children and len(duplicate_blocks) == 1
                    and duplicate_blocks[0].get("type") == "paragraph"
                    and "".join(
                        part.get("plain_text") or part.get("text", {}).get("content", "")
                        for part in (duplicate_blocks[0].get("paragraph") or {}).get("rich_text") or []
                    ) == LECTURE_PLACEHOLDER):
                client.archive_page(duplicate["id"], retry=False)
                children.pop(empty_title, None)
    children[base] = page
    return page, made


def _indexed_children(client: NotionClient, parent_id: str) -> dict[str, dict]:
    found = {}
    for page in client.list_child_pages(parent_id):
        title = page.get("title") or ""
        if title in found:
            raise ValueError(f"Notion 中有多个同名页面「{title}」，请先手动确认。")
        found[title] = page
    return found


def _ensure_indexed_child(client: NotionClient, parent_id: str,
                          title: str, children: dict[str, dict]) -> tuple[dict, bool]:
    if title in children:
        return children[title], False
    page = client.create_page(parent_id, title, retry=False)
    children[title] = {"id": page["id"], "title": title}
    return children[title], True


def _recover_published_notes(client: NotionClient, data_dir: Path, home_id: str,
                             course_id: str, lessons: list[dict],
                             lecture_titles: dict[str, str],
                             lecture_children: dict[str, dict]) -> int:
    """Adopt existing remote notes from an earlier release without republishing."""
    from datetime import datetime, timezone
    from ..pipeline import collect_jobs
    from .recordings_api import recording_id

    jobs = {recording_id(job): job for job in collect_jobs(data_dir, course_id)}
    recovered = 0
    for lesson in lessons:
        job = jobs.get(lesson["id"])
        if job is None or not (job.directory / "notes.md").is_file():
            continue
        marker = job.directory / "notion-publication.json"
        if marker.exists():
            continue
        lecture = lecture_children.get(lecture_titles[lesson["id"]])
        if not lecture:
            continue
        note_title = f"转写笔记 · {job.recording.date} {job.recording.title}"[:100]
        matches = [page for page in client.list_child_pages(lecture["id"])
                   if page.get("title") == note_title and page.get("id")]
        if len(matches) != 1:
            continue
        note_id = matches[0]["id"].replace("-", "")
        lecture_id = lecture["id"].replace("-", "")
        publication = {
            "recording_id": lesson["id"], "home_id": home_id,
            "lecture_id": lecture["id"],
            "lecture_url": f"https://www.notion.so/{lecture_id}",
            "note_url": f"https://www.notion.so/{note_id}",
            "recovered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        temporary = marker.with_name(marker.name + ".tmp")
        temporary.write_text(json.dumps(publication, ensure_ascii=False, indent=1), "utf-8")
        temporary.replace(marker)
        recovered += 1
    return recovered

SECTION_TITLES = ("通知", "作业", "课堂记录", "资料")
ASSESSMENT_TITLE = "考核评测"
LECTURE_PLACEHOLDER = "目录占位 · 这节录像尚未按需整理。当前没有课堂笔记；请在桌面课程页确认整理后再查看。"


def _source_state_path(data_dir: Path) -> Path:
    return data_dir / "notion-source-state.json"


def _source_state(data_dir: Path) -> dict:
    try:
        value = json.loads(_source_state_path(data_dir).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_source_state(data_dir: Path, value: dict) -> None:
    path = _source_state_path(data_dir)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), "utf-8")
    temporary.replace(path)


def _linked_paragraph(label: str, url: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": label, "link": {"url": url}}}
    ]}}


def _material_source_url(course_id: str, item: dict) -> str:
    from urllib.parse import quote
    import re
    parent_id = str(item.get("parent_content_id") or "")
    if re.fullmatch(r"_\d+_\d+", parent_id):
        return ("https://course.pku.edu.cn/webapps/blackboard/content/listContent.jsp"
                "?course_id=" + quote(course_id) + "&content_id=" + quote(parent_id))
    return "https://course.pku.edu.cn/"


def _source_url(course_id: str, section: str, item: dict) -> str:
    from urllib.parse import quote
    from .campus_todos import announcement_source_url
    base = "https://course.pku.edu.cn"
    if section == "作业" and item.get("source") == "handbook_review":
        return _material_source_url(course_id, item)
    if section == "作业" and item.get("source") == "announcement":
        return announcement_source_url(course_id, str(item.get("announcement_id") or ""))
    if section == "作业" and item.get("content_id") and item.get("source") != "calendar":
        return (base + "/webapps/assignment/uploadAssignment?course_id=" +
                quote(course_id) + "&content_id=" + quote(item["content_id"]))
    if section == "通知":
        return announcement_source_url(course_id, str(item.get("id") or ""))
    return _material_source_url(course_id, item)


def _summary_line(section: str, item: dict) -> str:
    if section == "作业":
        if item.get("source") == "handbook_review":
            return f"课程手册待核对 · {item.get('filename') or item.get('title') or '未命名手册'}"
        due = item.get("due_at_label") or item.get("due_at")
        if not due and item.get("teacher_deadline_quote"):
            due = ("老师原文截止线索 " + str(item["teacher_deadline_quote"])
                   + "（系统截止字段空）")
        due = due or "未公布"
        source = "公告待办" if item.get("source") == "announcement" else "正式作业"
        return f"{source} · 截止：{due} · {item.get('title') or '未命名任务'}"
    if section == "通知":
        return f"老师通知 · {item.get('title') or '未命名通知'}"
    return f"课程资料 · {item.get('title') or '未命名资料'}"


def _item_blocks(course_id: str, section: str, item: dict, synced_at: str) -> list[dict]:
    summary = _summary_line(section, item)
    lines = [summary]
    body = item.get("body_text") or ""
    if section == "作业":
        if item.get("source") == "handbook_review":
            lines.extend([
                "课程手册要求，需向老师或助教核对最新安排；以下不是教学网正式作业卡片。",
                "本卡不提供已验证的系统截止时间或提交入口。",
                str(item.get("status") or "请核对手册原文。"),
            ])
            if item.get("time_preview"):
                lines.append("手册原文中的时间／提交线索（未核对是否仍有效）：")
                for cue in item["time_preview"]:
                    lines.append(f"PDF 第 {cue['page']} 页 · {cue['text']}")
            if item.get("presence_preview"):
                lines.append("笔记无法代替的参与事项（到场方式待核对）：")
                for cue in item["presence_preview"]:
                    lines.append(f"{cue['label']} · PDF 第 {cue['page']} 页 · {cue['text']}\n{cue['note']}")
            for page in item.get("pages") or []:
                lines.append(f"PDF 第 {page['page']} 页 · 手册原文摘录：\n{page['excerpt']}"
                             + ("\n摘录未显示整页，请打开 PDF 核对。" if page.get("truncated") else ""))
        elif item.get("source") == "announcement":
            lines.extend(["由老师公告提取的待办；这不是独立的教学网提交入口。",
                          f"老师原话：{item.get('instructions') or '请核对原公告'}"])
        else:
            due = item.get("due_at_label") or item.get("due_at")
            if not due and item.get("teacher_deadline_quote"):
                due = ("老师原文写 " + str(item["teacher_deadline_quote"])
                       + "；教学网未给独立截止字段，请打开原作业页核对")
            lines.extend([f"要交什么：{item.get('instructions') or '请打开原要求核对'}",
                          f"截止时间：{due or '未公布'}"])
        body = ""
    elif section == "通知":
        lines.append(f"发布时间：{item.get('posted_at_label') or item.get('posted_at') or '未提供'}")
    else:
        lines.append(f"所在目录：{item.get('path') or '未提供'}")
    files = item.get("files") or []
    if files:
        lines.append("文件：" + "、".join(str(name) for name in files))
    if section in {"作业", "通知"} and _PRESENCE_REQUIRED.search(
            " ".join(str(item.get(field) or "") for field in ("title", "instructions", "body_text"))):
        lines.append("需要到场或课堂参与：老师要求涉及考勤或现场活动；笔记不能代替本人参与，请核对原要求。")
    if body:
        lines.append(body)
    blocks = markdown_to_blocks("\n\n".join(lines))
    linked_files = 0
    for asset in item.get("attachments") or []:
        if not isinstance(asset, dict):
            continue
        path = _safe_attachment_path(asset.get("path"))
        if path:
            linked_files += 1
            blocks.append(_linked_paragraph(
                ("登录教学网打开手册 PDF：" if section == "作业" and item.get("source") == "handbook_review" else
                 "登录教学网查看原题：" if section == "作业" else "打开或下载 ")
                + str(asset.get("filename") or "附件"),
                "https://course.pku.edu.cn" + path))
    if section == "作业":
        from ..materials import safe_source_link

        for source_link in item.get("source_links") or []:
            if not isinstance(source_link, dict):
                continue
            safe = safe_source_link(source_link.get("label"), source_link.get("url"))
            if safe:
                blocks.append(_linked_paragraph("老师原页面链接：" + safe.label, safe.url))
    if section == "作业" and item.get("source") == "handbook_review" and not linked_files:
        blocks += markdown_to_blocks(
            "当前教学网索引未提供这份 PDF 的安全直达链接；请在桌面版课程页下载原文件，或登录教学网按上方文件名查找。"
        )
    if section == "作业" and item.get("source") not in {"announcement", "handbook_review"}:
        if files and not linked_files:
            blocks += markdown_to_blocks(
                "原题附件尚无可用直达链接；请登录教学网，从下方提交目标核对并下载原题。"
            )
        elif linked_files:
            blocks += markdown_to_blocks(
                "原题链接需要教学网登录；请打开原文件核对完整题目，笔记不能代替原题。"
            )
    label = ("打开教学网（登录后按文件名查找手册）" if section == "作业" and item.get("source") == "handbook_review" else
             "打开教学网作业页面（提交或查看历史）" if section == "作业" and item.get("content_id")
             and item.get("source") != "calendar" else
             "打开老师公告原文" if section == "作业" and item.get("source") == "announcement" else
             "打开这条通知" if section == "通知" else
             "打开教学网原目录")
    blocks.append(_linked_paragraph(label, _source_url(course_id, section, item)))
    blocks += markdown_to_blocks(
        f"请以教学网原页面为准。目录读取：{synced_at or '未记录'}。课程标识：{course_id}。"
    )
    return blocks


def _source_items(course: dict, section: str) -> list[dict]:
    if section == "通知":
        return course["announcements"]
    if section == "作业":
        return course["assignments"] + course.get("announcement_tasks", []) + course.get("handbook_reviews", [])
    return [item for item in course["all_materials"] if item["category"] == "material"]


def catalog_plan_total(rows: list[dict]) -> int:
    """Work items a catalog sync will visit, so the bar reflects real work.

    The counts come from the same local course index the sync walks, so the
    planned total and the reported progress use one definition of "an item".
    """
    total = 1  # the dashboard snapshot written once at the end
    for row in rows:
        total += len(SECTION_TITLES)  # 通知 / 作业 / 课堂记录 / 资料 directory pages
        total += int(row.get("announcements") or 0)
        total += (int(row.get("assignments") or 0) + int(row.get("announcement_tasks") or 0)
                  + int(row.get("handbook_reviews") or 0))
        total += int(row.get("materials") or 0)
        total += 1  # 考核评测 page
        total += 1  # 已有课程笔记 link page
        total += int(row.get("recordings") or 0)
        total += 1  # 课堂记录 index
    return total


def _source_key(course_id: str, section: str, item: dict) -> str:
    if section == "作业" and item.get("source") == "handbook_review":
        return course_id + "|作业|handbook|" + str(item.get("material_path") or "") + "|" + str(item.get("filename") or "")
    identity = (item.get("id") if section == "通知" else
                (item.get("id") if item.get("source") == "announcement" else item.get("content_id")) if section == "作业" else item.get("path"))
    identity = identity or (item.get("title", "") +
                            ("|" + str(item.get("posted_at") or "") if section == "通知" else ""))
    return course_id + "|" + section + "|" + str(identity)


_HISTORICAL_SOURCE_NOTE = (
    "以下是旧版同步记录或手动修改的内容，可能已过期；"
    "本页当前要求以上方内容及教学网原页面为准。"
)


def _source_block_signature(block: dict) -> str:
    """Compare app-written content without relying on Notion response metadata."""
    kind = block.get("type") or ""
    rich = (block.get(kind) or {}).get("rich_text") or []
    parts = []
    for part in rich:
        value = part.get("text") or {}
        link = value.get("link") or part.get("href") or {}
        parts.append((part.get("type"), value.get("content") or part.get("plain_text") or "",
                      link.get("url") if isinstance(link, dict) else link))
    return hashlib.sha256(json.dumps((kind, parts), ensure_ascii=False,
                                   sort_keys=True).encode("utf-8")).hexdigest()


def _source_block_state(blocks: list[dict]) -> dict[str, str]:
    return {block["id"]: _source_block_signature(block)
            for block in blocks if block.get("id")}


def _recover_source_append(client: NotionClient, page_id: str,
                           blocks: list[dict], owned_before: dict,
                           pending: dict) -> list[dict]:
    """Remove only unchanged blocks from an interrupted app append.

    The intent is saved before the PATCH. Once the PATCH returns, its block IDs
    are saved before any other mutation. A lost PATCH response has no IDs, so
    only its exact, ordered prefix immediately after the card heading is safe
    to recognize. Edited blocks are never archived by recovery.
    """
    if pending.get("id") != page_id or not blocks:
        return blocks
    created = pending.get("created_blocks") or {}
    if isinstance(created, dict) and created:
        for block in blocks[1:]:
            block_id = block.get("id")
            if block_id in created and _source_block_signature(block) == created[block_id]:
                client.archive_block(block_id, retry=False)
    else:
        expected = pending.get("fresh_signatures") or []
        if not isinstance(expected, list):
            expected = []
        tracked_ids = set(owned_before) if isinstance(owned_before, dict) else set()
        for block, signature in zip(blocks[1:], expected):
            block_id = block.get("id")
            if not block_id or block_id in tracked_ids:
                break
            if _source_block_signature(block) == signature:
                client.archive_block(block_id, retry=False)
    return client.list_children(page_id)


def _next_formal_assignment_due(course: dict, now: datetime | None = None) -> str:
    """Summarize only a future, timezone-aware deadline from the formal index."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Deadline comparison requires an aware current time.")
    candidates: list[tuple[datetime, str, str]] = []
    for item in course.get("assignments") or []:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("due_at") or "").strip()
        try:
            due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if due.tzinfo is None or due < current:
            continue
        label = display_campus_time(raw)
        if not label.endswith("北京时间"):
            continue
        candidates.append((due, str(item.get("title") or "未命名作业"), label))
    if not candidates:
        return ""
    _, title, label = min(candidates)
    return f"最近已知正式作业截止：{label} · {title}。请打开对应作业卡核对原题与提交要求。"


def _sync_source_status(client: NotionClient, page_id: str,
                        section: str, course: dict, count: int,
                        now: datetime | None = None) -> None:
    if section == "通知" and course.get("announcements_status") == "not_synced":
        status = "教学网状态：通知尚未同步；不能据此判断没有通知。"
    elif section == "通知" and course.get("announcements_status") == "error":
        status = "教学网状态：通知读取失败；下方条目可能是旧缓存，请到教学网核对。"
    elif section == "通知" and course.get("announcements_status") == "unavailable":
        status = "教学网状态：课程未开放通知接口；不能据此判断没有通知。"
    elif section == "作业" and course.get("assignments_status") == "not_synced":
        status = "教学网状态：正式作业尚未同步；不能据此判断没有作业。"
    elif section == "作业" and course.get("assignments_status") in {"partial", "error"}:
        status = "教学网状态：作业来源读取不完整；下方条目可能遗漏，请到教学网核对。"
    elif not course.get("synced_at"):
        status = "教学网状态：目录尚未更新；不能据此判断没有正式内容。"
    elif section == "作业" and course.get("handbook_reviews"):
        status = (f"教学网状态：{course.get('synced_at_label') or course['synced_at']} "
                  f"读取到 {len(course.get('assignments', []))} 项正式作业、"
                  f"{len(course.get('announcement_tasks', []))} 项公告待办；"
                  f"另有 {len(course['handbook_reviews'])} 份课程手册待核对，手册要求可能未进入正式作业索引。")
    else:
        status = (f"教学网状态：{course.get('synced_at_label') or course['synced_at']} 读取到 {count} 项{section}；"
                  "请以教学网原页面为准。")
    if (section == "作业" and course.get("assignments_status") == "available"
            and course.get("synced_at")):
        nearest = _next_formal_assignment_due(course, now)
        if nearest:
            status += "\n" + nearest
        teacher_clues = [
            f"{item.get('title') or '未命名作业'} · {item['teacher_deadline_quote']}"
            for item in course.get("assignments") or []
            if isinstance(item, dict) and not item.get("due_at")
            and item.get("teacher_deadline_quote")
        ]
        if teacher_clues:
            status += ("\n老师原文中的截止线索（系统无独立截止字段，请打开原作业页核对）："
                       + "；".join(teacher_clues[:3]))
    for block in client.list_children(page_id):
        if block.get("type") != "paragraph":
            continue
        rich = (block.get("paragraph") or {}).get("rich_text") or []
        previous = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                           for part in rich)
        if previous.startswith("教学网状态："):
            if previous != status:
                client.update_paragraph(block["id"], status)
            return
    client.append_blocks(page_id, markdown_to_blocks(status), retry=False)


def _sync_source_section(client: NotionClient, data_dir: Path, course_id: str,
                         section: str, page_id: str, course: dict, state: dict,
                         tick=None) -> None:
    children = _indexed_children(client, page_id)
    items = _source_items(course, section)
    title_counts = Counter(str(item.get("title") or "未命名条目")[:86] for item in items)
    for item in items:
        if tick:
            tick(f"{section}：" + str(item.get("title") or "未命名条目")[:40])
        key = _source_key(course_id, section, item)
        base_title = str(item.get("title") or "未命名条目")[:86]
        title = (base_title + " · " + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
                 if title_counts[base_title] > 1 else base_title)
        digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True)
                                .encode("utf-8")).hexdigest()
        previous = state.get(key) if isinstance(state.get(key), dict) else {}
        existing = children.get(title)
        blocks = _item_blocks(course_id, section, item,
                              course.get("synced_at_label") or course.get("synced_at", ""))
        if existing is None:
            created = client.create_page(page_id, title, children=blocks, retry=False)
            children[title] = {"id": created["id"], "title": title}
            page_id_for_item = created["id"]
            owned = _source_block_state(client.list_children(page_id_for_item))
            legacy = False
        else:
            page_id_for_item = existing["id"]
            existing_blocks = client.list_children(page_id_for_item)
            first = existing_blocks[0] if existing_blocks else {}
            rich = (first.get(first.get("type") or "", {}) or {}).get("rich_text") or []
            marker = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                             for part in rich)
            owned_before = previous.get("owned_blocks") or {}
            pending = previous.get("pending") if isinstance(previous.get("pending"), dict) else {}
            if pending:
                if pending.get("id") != page_id_for_item:
                    raise ValueError(f"Notion 卡片「{title}」的未完成同步目标不一致；未覆盖。")
                first_signature = _source_block_signature(first)
                if (first.get("id") in owned_before and
                        first_signature not in {owned_before[first["id"]],
                                                pending.get("summary_signature")}):
                    raise ValueError(f"Notion 卡片「{title}」的顶部已被手动修改；未覆盖。")
                existing_blocks = _recover_source_append(
                    client, page_id_for_item, existing_blocks, owned_before, pending)
                first = existing_blocks[0] if existing_blocks else {}
                rich = (first.get(first.get("type") or "", {}) or {}).get("rich_text") or []
                marker = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                                 for part in rich)
            if previous.get("id") != page_id_for_item:
                if not (marker.startswith(f"来源：教学网正式{section}目录") or
                        marker.startswith(_summary_line(section, item))):
                    raise ValueError(f"Notion 中存在同名的手动页面「{title}」，未覆盖。")
            elif previous.get("layout") in {3, 4} and first.get("id") in owned_before:
                if (_source_block_signature(first) != owned_before[first["id"]] and
                        _source_block_signature(first) != pending.get("summary_signature")):
                    raise ValueError(f"Notion 卡片「{title}」的顶部已被手动修改；未覆盖。")
            elif not (marker.startswith(f"来源：教学网正式{section}目录") or
                      marker.startswith(_summary_line(section, item).split(" · ")[0])):
                raise ValueError(f"Notion 卡片「{title}」的顶部已被手动修改；未覆盖。")

            if (previous.get("id") == page_id_for_item and
                    previous.get("layout") == 4 and previous.get("hash") == digest
                    and not pending):
                continue

            # Layout 3 could own repeated historical copies from older syncs.
            # Rebuild it once from the current source and archive only blocks
            # whose recorded signatures still match. Untracked or edited blocks
            # stay below the current requirements as possible student content.
            current_ids = {block["id"] for block in existing_blocks if block.get("id")}
            tracked_ids = set(owned_before) if isinstance(owned_before, dict) else set()
            legacy = bool(current_ids - tracked_ids - {first.get("id")})
            to_archive = []
            for block in existing_blocks[1:]:
                block_id = block.get("id")
                if block_id in tracked_ids:
                    if _source_block_signature(block) == owned_before[block_id]:
                        to_archive.append(block_id)
                    else:
                        legacy = True
            fresh = blocks[1:]
            if legacy:
                fresh += markdown_to_blocks(_HISTORICAL_SOURCE_NOTE)
            pending = {"id": page_id_for_item,
                       "summary_signature": _source_block_signature(blocks[0]),
                       "fresh_signatures": [_source_block_signature(block) for block in fresh]}
            state[key] = {**previous, "pending": pending}
            if data_dir is not None:
                _save_source_state(data_dir, state)
            # Notion may return the full child list from PATCH, including old
            # blocks. Determine ownership from a fresh page read, never from
            # that response, and require the exact inserted order/content.
            client.append_blocks(page_id_for_item, fresh,
                                 after=first["id"], retry=False)
            after_append = client.list_children(page_id_for_item)
            added = [block for block in after_append
                     if block.get("id") and block["id"] not in current_ids]
            added_ids = [block["id"] for block in added]
            if (len(added) != len(fresh) or
                    added_ids != [block.get("id") for block in
                                  after_append[1:1 + len(fresh)]] or
                    [_source_block_signature(block) for block in added] !=
                    pending["fresh_signatures"]):
                raise ValueError(f"Notion 卡片「{title}」的新块未按预期写入；已停止同步以保留原内容。")
            pending["created_blocks"] = _source_block_state(added)
            if data_dir is not None:
                _save_source_state(data_dir, state)
            client.update_paragraph(first["id"], _summary_line(section, item))
            for block_id in to_archive:
                client.archive_block(block_id, retry=False)
            owned = {first["id"]: _source_block_signature(blocks[0])}
            owned.update(_source_block_state(added))
        state[key] = {"id": page_id_for_item, "hash": digest, "layout": 4,
                      "owned_blocks": owned, "legacy": legacy}
    _sync_source_status(client, page_id, section, course, len(items))


def _sync_existing_notes_link(client: NotionClient, course_id: str,
                              course_page_id: str, course: dict, state: dict) -> None:
    from urllib.parse import urlsplit
    url = course.get("existing_notion_url") or ""
    if not url:
        return
    host = urlsplit(url).hostname or ""
    if not (host in {"notion.so", "www.notion.so", "app.notion.com"}
            or host.endswith(".notion.site")):
        raise ValueError("已有课程笔记页不是有效的 Notion 链接。")
    page = _child(client, course_page_id, "已有课程笔记")
    key = course_id + "|已有课程笔记"
    if state.get(key) == url:
        return
    marker = "这是你手动关联的既有 Notion 课程页；本应用不会修改该页面。"
    blocks = markdown_to_blocks(marker)
    blocks.append(_linked_paragraph("打开既有课程笔记", url))
    existing = client.list_children(page["id"])
    if existing:
        first = existing[0]
        rich = (first.get(first.get("type") or "", {}) or {}).get("rich_text") or []
        text = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                       for part in rich)
        if text != marker:
            raise ValueError("已有课程笔记的链接页含有手动内容，已停止自动覆盖。")
        client.replace_page_content(page["id"], blocks)
    else:
        client.append_blocks(page["id"], blocks, retry=False)
    state[key] = url


def _sync_lecture_placeholder(client: NotionClient, lecture_id: str) -> None:
    if not client.list_children(lecture_id):
        client.append_blocks(lecture_id, markdown_to_blocks(LECTURE_PLACEHOLDER),
                             retry=False)


def _sync_recording_section(client: NotionClient, page_id: str,
                            lessons: list[dict], titles: dict[str, str],
                            lecture_children: dict[str, dict], state: dict,
                            course_id: str) -> None:
    rows = []
    for lesson in lessons:
        page = lecture_children[titles[lesson["id"]]]
        note_children = client.list_child_pages(page["id"])
        has_note = _has_note_child(note_children)
        status = "有转写笔记，待质量验收" if has_note else "待整理，仅有目录"
        rows.append((titles[lesson["id"]], page["id"], status))
    rows.sort(key=lambda row: (row[2] != "有转写笔记，待质量验收", row[0]))
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()
    key = course_id + "|课堂记录"
    if state.get(key) == digest:
        return
    blocks = markdown_to_blocks(
        "课堂页按教学网录像目录建立。待整理表示没有笔记；有转写笔记仍需核对整节内容、课件、老师要求和正式作业，不能仅凭页面存在判断已验收。"
    )
    for title, lecture_id, status in rows:
        blocks.append(_linked_paragraph(f"{title} · {status}",
                                        "https://www.notion.so/" + lecture_id.replace("-", "")))
    existing = client.list_children(page_id)
    if existing:
        first = existing[0]
        rich = (first.get(first.get("type") or "", {}) or {}).get("rich_text") or []
        marker = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                         for part in rich)
        if not marker.startswith("课堂页按教学网录像目录建立"):
            raise ValueError("课堂记录索引含有手动内容，已停止自动覆盖。")
        client.replace_page_content(page_id, blocks)
    else:
        client.append_blocks(page_id, blocks, retry=False)
    state[key] = digest



def _sync_assessment_page(client: NotionClient, data_dir: Path, course_id: str,
                          course_page_id: str, detail: dict, state: dict) -> None:
    """Keep one course-level page for grading, exam and submission rules.

    A lecture note records the lecture. The rules a student must not miss are
    course-level and scattered across the handbook, the formal cards and the
    teacher's own words, so they get one page of their own. The page is rebuilt
    from its sources, so it refuses to touch a page edited by hand.
    """
    from .assessment_page import INTRO, assessment_markdown, collect_spoken_lines

    markdown = assessment_markdown(detail, collect_spoken_lines(data_dir, course_id))
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    key = course_id + "|考核评测"
    children = _indexed_children(client, course_page_id)
    page, _made = _ensure_indexed_child(client, course_page_id, ASSESSMENT_TITLE,
                                        children)
    if state.get(key) == digest:
        return
    blocks = markdown_to_blocks(markdown)
    existing = client.list_children(page["id"])
    if existing:
        first = existing[0]
        rich = (first.get(first.get("type") or "", {}) or {}).get("rich_text") or []
        marker = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                         for part in rich)
        if not marker.startswith(INTRO[:24]):
            raise ValueError("考核评测页含有手动内容，已停止自动覆盖。")
        client.replace_page_content(page["id"], blocks)
    else:
        client.append_blocks(page["id"], blocks, retry=False)
    state[key] = digest


DASHBOARD_TITLE = "近期待办与新通知"
DASHBOARD_MARKER = "学习概览 · 来自教学网正式目录与老师公告；请以原页面为准。"


def _dashboard_snapshot_label(courses: list[dict]) -> str:
    """Keep the source age visible even if nobody opens the desktop app again."""
    timestamps = []
    for course in courses:
        raw = str(course.get("synced_at") or "")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            timestamps.append(parsed.astimezone(timezone.utc))
    if not timestamps:
        source = "教学网课程目录的读取时间未记录。"
    else:
        earliest = display_campus_time(min(timestamps).isoformat())
        latest = display_campus_time(max(timestamps).isoformat())
        source = (f"教学网课程目录读取时间：{earliest}。" if earliest == latest else
                  f"教学网课程目录读取时间：{earliest} 至 {latest}。")
    missing = len(courses) - len(timestamps)
    if missing:
        source += f"另有 {missing} 门课程未记录读取时间。"
    return (source + "此页是同步快照，不会实时刷新；超过 24 小时的课程请在桌面版更新目录，"
            "并到教学网原页核对新作业与通知。")


def _dashboard_blocks(data_dir: Path, titles: dict[str, str], state: dict) -> list[dict]:
    from datetime import timedelta
    from .campus_todos import announcement_source_url
    now = datetime.now(timezone.utc)
    due_rows = []
    notice_rows = []
    handbook_rows = []
    course_rows = campus_courses(data_dir)["courses"]
    for row in course_rows:
        course = campus_course(data_dir, row["id"])
        course_title = titles[row["id"]]
        for item in course["assignments"] + course.get("announcement_tasks", []):
            due_raw = str(item.get("due_at") or "")
            if not due_raw:
                continue
            try:
                due = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone(timedelta(hours=8)))
            except ValueError:
                continue
            if due < now or due > now + timedelta(days=14):
                continue
            key = _source_key(row["id"], "作业", item)
            page_id = (state.get(key) or {}).get("id", "")
            url = ("https://www.notion.so/" + page_id.replace("-", "")
                   if page_id else _source_url(row["id"], "作业", item))
            due_rows.append((due, course_title, item, url))
        for item in course.get("handbook_reviews", []):
            key = _source_key(row["id"], "作业", item)
            page_id = (state.get(key) or {}).get("id", "")
            url = ("https://www.notion.so/" + page_id.replace("-", "")
                   if page_id else _source_url(row["id"], "作业", item))
            handbook_rows.append((course_title, item, url))
        for item in course["announcements"]:
            key = _source_key(row["id"], "通知", item)
            page_id = (state.get(key) or {}).get("id", "")
            url = ("https://www.notion.so/" + page_id.replace("-", "") if page_id
                   else announcement_source_url(row["id"], str(item.get("id") or "")))
            notice_rows.append((str(item.get("posted_at") or ""), course_title, item, url))
    snapshot_day = now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    blocks = markdown_to_blocks(
        DASHBOARD_MARKER + "\n\n" + _dashboard_snapshot_label(course_rows) +
        f"\n\n## {snapshot_day} 起 14 天内的已知截止事项"
    )
    for _, course_title, item, url in sorted(due_rows, key=lambda value: value[0]):
        kind = "公告待办" if item.get("source") == "announcement" else "正式作业"
        label = (f"{item.get('due_at_label') or item.get('due_at')} · {course_title} · "
                 f"{kind} · {item.get('notice_title') or item.get('title')}")
        blocks.append(_linked_paragraph(label, url))
    if not due_rows:
        blocks += markdown_to_blocks("正式作业和公告索引中未发现未来 14 天的已知截止事项；课程手册及课堂要求仍需核对。")
    if handbook_rows:
        time_rows = [(title, item, url, cue)
                     for title, item, url in handbook_rows
                     for cue in (item.get("time_preview") or [])[:2]][:12]
        if time_rows:
            blocks += markdown_to_blocks(
                "## 手册中的日期线索（节选，待核对）\n\n"
                "以下保留手册原文和页码，可能包含小测、展示或提交安排；"
                "尚未核对年份、本人参与方式或老师后续调整，不代表正式作业截止时间。"
                "点开手册卡片查看其余要求。")
            for course_title, item, url, cue in time_rows:
                excerpt = str(cue.get("text") or "").strip().replace("\n", " / ")
                if len(excerpt) > 600:
                    excerpt = excerpt[:600] + "…（原文见手册）"
                blocks.append(_linked_paragraph(
                    f"{course_title} · {item.get('filename')} · PDF 第 {cue['page']} 页 · {excerpt}", url))
        presence_rows = [(title, item, url) for title, item, url in handbook_rows
                         if item.get("presence_preview")]
        if presence_rows:
            blocks += markdown_to_blocks("## 需核对本人参与的事项\n\n"
                                         "笔记不能代替出勤、现场活动或本人考试；具体到场和参与方式以老师最新要求为准。")
            for course_title, item, url in presence_rows:
                labels = "、".join(dict.fromkeys(cue["label"] for cue in item["presence_preview"]))
                blocks.append(_linked_paragraph(
                    f"{course_title} · {labels} · {item.get('filename')}（查看手册原文与页码）", url))
        blocks += markdown_to_blocks("## 课程手册待核对")
        for course_title, item, url in handbook_rows:
            pages = "、".join(str(page["page"]) for page in item.get("pages") or [])
            label = f"{course_title} · {item.get('filename')}" + (f" · PDF 第 {pages} 页有考核线索" if pages else " · 请核对全文")
            blocks.append(_linked_paragraph(label, url))
    blocks += markdown_to_blocks("## 最近通知")
    for _, course_title, item, url in sorted(notice_rows, reverse=True)[:10]:
        blocks.append(_linked_paragraph(
            f"{course_title} · {item.get('title') or '未命名通知'} · "
            f"{item.get('posted_at_label') or item.get('posted_at') or '时间未提供'}", url))
    if not notice_rows:
        blocks += markdown_to_blocks("尚未同步到正式通知；请到教学网核对。")
    return blocks


def _sync_dashboard(client: NotionClient, data_dir: Path, home_id: str,
                    titles: dict[str, str], state: dict) -> None:
    blocks = _dashboard_blocks(data_dir, titles, state)
    digest = hashlib.sha256(json.dumps(blocks, ensure_ascii=False, sort_keys=True)
                            .encode("utf-8")).hexdigest()
    page = _child(client, home_id, DASHBOARD_TITLE)
    previous = state.get("|dashboard") if isinstance(state.get("|dashboard"), dict) else {}
    existing = client.list_children(page["id"])
    if existing:
        first = existing[0]
        rich = (first.get(first.get("type") or "", {}) or {}).get("rich_text") or []
        marker = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                         for part in rich)
        if marker != DASHBOARD_MARKER:
            raise ValueError("近期待办页面已有手动内容；未自动覆盖。")
        if previous.get("hash") != digest:
            owned_ids = previous.get("block_ids") or []
            if owned_ids and owned_ids != [block["id"] for block in existing]:
                raise ValueError("近期待办页面包含手动编辑；未自动覆盖。")
            client.replace_page_content(page["id"], blocks)
    else:
        client.append_blocks(page["id"], blocks, retry=False)
    home_blocks = client.list_children(home_id)
    link_text = "查看近期待办与新通知"
    has_link = any(
        block.get("type") == "paragraph" and
        any((part.get("plain_text") or part.get("text", {}).get("content", "")) == link_text
            for part in (block.get("paragraph") or {}).get("rich_text") or [])
        for block in home_blocks
    )
    if not has_link:
        first = home_blocks[0]["id"] if home_blocks else None
        client.append_blocks(home_id, [_linked_paragraph(
            link_text, "https://www.notion.so/" + page["id"].replace("-", "")
        )], after=first, retry=False)
    current = client.list_children(page["id"])
    state["|dashboard"] = {"id": page["id"], "hash": digest,
                           "block_ids": [block["id"] for block in current]}

def sync_catalog(data_dir: Path, token: str, stage=None, progress=None,
                 item_progress=None) -> dict:
    """Create empty Notion course and lecture pages from the campus index.

    This operation never downloads recordings or attachments. Repeated runs
    inspect existing children before writing, so refreshing a course is safe.
    ``item_progress(done, total, label)`` reports how many planned pages and
    cards are finished, so the desktop panel can show remaining work honestly.
    """
    home = saved_home(data_dir)
    if not home:
        raise ValueError("请先选择 Notion 父页面并创建学习主页。")
    rows = campus_courses(data_dir)["courses"]
    titles = _course_titles(data_dir)
    created_courses = created_lectures = recovered_notes = 0
    total_items = catalog_plan_total(rows)
    done_items = 0

    def tick(label: str) -> None:
        nonlocal done_items
        done_items += 1
        if item_progress:
            item_progress(done_items, total_items, label)

    source_state = _source_state(data_dir)
    with NotionClient(token) as client:
        client.get_page(home["id"])
        home_children = _indexed_children(client, home["id"])
        for index, row in enumerate(rows, 1):
            if stage:
                stage(f"建立 Notion 课程页：{row['title']}")
            course_page, made = _ensure_indexed_child(
                client, home["id"], titles[row["id"]], home_children
            )
            created_courses += int(made)
            detail = campus_course(data_dir, row["id"])
            course_children = _indexed_children(client, course_page["id"])
            sections = {}
            for section in SECTION_TITLES:
                section_page, _ = _ensure_indexed_child(
                    client, course_page["id"], section, course_children
                )
                sections[section] = section_page
                tick(f"{row['title']} · 建目录页：{section}")
            for section in ("通知", "作业", "资料"):
                _sync_source_section(client, data_dir, row["id"], section,
                                     sections[section]["id"], detail, source_state,
                                     tick=lambda label, course_title=row["title"]:
                                     tick(f"{course_title} · {label}"))
            _sync_assessment_page(client, data_dir, row["id"], course_page["id"],
                                  detail, source_state)
            tick(f"{row['title']} · 考核评测页")
            _sync_existing_notes_link(client, row["id"], course_page["id"],
                                      detail, source_state)
            tick(f"{row['title']} · 已有课程笔记页")
            lecture_titles = _lecture_titles(detail)
            lecture_children = _indexed_children(client, course_page["id"])
            for lesson in detail["lessons"]:
                lecture_page, made = _ensure_status_lecture(
                    client, course_page["id"], lecture_titles[lesson["id"]], lecture_children
                )
                created_lectures += int(made)
                _sync_lecture_placeholder(client, lecture_page["id"])
                tick(f"{row['title']} · 讲次页："
                     + " · ".join(part for part in (lesson["date"], lesson["title"]) if part))
            recovered_notes += _recover_published_notes(
                client, data_dir, home["id"], row["id"], detail["lessons"],
                lecture_titles, lecture_children
            )
            _sync_recording_section(client, sections["课堂记录"]["id"],
                                    detail["lessons"], lecture_titles,
                                    lecture_children, source_state, row["id"])
            tick(f"{row['title']} · 课堂记录索引")
            _save_source_state(data_dir, source_state)
            if progress:
                progress(index, len(rows), "courses", 0)
        _sync_dashboard(client, data_dir, home["id"], titles, source_state)
        _save_source_state(data_dir, source_state)
        tick("汇总近期待办与新通知")
    return {"courses": len(rows), "created_courses": created_courses,
            "created_lectures": created_lectures, "recovered_notes": recovered_notes, "home_url": home.get("url", "")}


def ensure_recording_target(data_dir: Path, token: str, job) -> str:
    home = saved_home(data_dir)
    if not home:
        raise ValueError("请先在 Notion 中创建学习主页。")
    course = campus_course(data_dir, job.recording.course_id)
    from .recordings_api import recording_id
    lecture_title = _lecture_titles(course)[recording_id(job)]
    course_title = _course_titles(data_dir)[job.recording.course_id]
    with NotionClient(token) as client:
        client.get_page(home["id"])
        course_page = _child(client, home["id"], course_title)
        children = _indexed_children(client, course_page["id"])
        lecture_page = _find_lecture(children, lecture_title)
        if lecture_page is None:
            lecture_page, _ = _ensure_status_lecture(
                client, course_page["id"], lecture_title, children)
    return lecture_page["id"]

