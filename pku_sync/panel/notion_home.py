"""Create the student's Notion learning tree only after an explicit choice."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from contextlib import contextmanager, ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from ..notion import NotionClient, markdown_to_blocks
from ..projection_receipts import verified_workspace_operation
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


def legacy_home(data_dir: Path) -> dict:
    try:
        home = json.loads(home_path(data_dir).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return home if isinstance(home, dict) and home.get("id") and home.get("parent_id") else {}


def saved_home(data_dir: Path) -> dict:
    from .notion_workspace import saved_workspace
    workspace = saved_workspace(data_dir)
    return workspace if workspace.get("active") else legacy_home(data_dir)


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


def move_home(data_dir: Path, token: str, parent_id: str, *, settings=None) -> dict:
    """Move the existing learning tree, preserving its page ID and contents."""
    existing = saved_home(data_dir)
    if existing.get("schema") == 2:
        from .notion_workspace import move_workspace
        return move_workspace(settings, parent_id)
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
                          title: str, children: dict[str, dict], *,
                          initial_blocks: list[dict] | None = None) -> tuple[dict, bool]:
    if title in children:
        return children[title], False
    options = {'children': initial_blocks} if initial_blocks else {}
    page = client.create_page(parent_id, title, retry=False, **options)
    if initial_blocks:
        actual = _document_blocks(client, page['id'])
        if [_document_signature(row) for row in actual] != [_document_signature(row) for row in initial_blocks]:
            raise ValueError(f"Notion 页面「{title}」创建后验证失败；未记录页面身份。")
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


def _save_source_state(data_dir: Path, value: dict, *, path: Path | None = None) -> None:
    path = path or _source_state_path(data_dir)
    if not path.resolve().is_relative_to(data_dir.resolve()):
        raise ValueError('Notion 恢复记录不在当前本机数据目录中。')
    path.parent.mkdir(parents=True, exist_ok=True)
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
    if section == '测验':
        return '教学网正式测验 · ' + str(item.get('title') or '未命名测验')
    if section == "作业":
        if item.get("source") == "calendar":
            due = item.get("due_at_label") or item.get("due_at") or "未提供"
            return f"教学网日历事项 · 日历时间：{due} · {item.get('title') or '未命名事项'}"
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
        elif item.get("source") == "calendar":
            lines.extend(["尚未关联到正式作业；日历时间不能据此认定为作业截止时间。",
                          "本卡没有已验证的提交目标，请到教学网核对事项原文。",
                          str(item.get("instructions") or "")])
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
    elif section == '测验':
        lines.append('教学网原目录中的测验，需在教学网完成。AI 学习小测不会代替正式测验。')
        lines.append(str(item.get('status_label') or '截止时间、作答与提交状态请到教学网核对。'))
        lines.append('所在目录：' + str(item.get('path') or '未提供'))
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
    if item.get("source_id"):
        from urllib.parse import quote
        identity = quote(_source_key(course_id, section, item), safe="")
        blocks.append(_linked_paragraph("查看教学网同步来源",
                      "https://course.pku.edu.cn/#pku-card/" + identity + "/" + _source_block_signature(blocks[0])))
    return blocks


def _source_items(course: dict, section: str) -> list[dict]:
    if section == '测验':
        return course.get('official_assessments', [])
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
    if item.get("source_id"):
        return course_id + "|" + section + "|" + item["source_id"]
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


def _document_signature(block: dict) -> str:
    kind = block.get("type", "")
    body = block.get(kind) or {}
    details = {}
    if kind == "table":
        details = {key: body.get(key) for key in ("table_width", "has_column_header", "has_row_header")}
        details["rows"] = [_document_signature(row) for row in body.get("children", [])]
    elif kind == "table_row":
        details["cells"] = [_source_block_signature({"type": "paragraph", "paragraph": {"rich_text": cell}})
                            for cell in body.get("cells", [])]
    elif kind == "equation":
        details["expression"] = body.get("expression")
    details["styles"] = [{key: value for key, value in (part.get("annotations") or {}).items()
                          if value is True or (key == "color" and value != "default")}
                         for part in body.get("rich_text", [])]
    return hashlib.sha256(json.dumps((_source_block_signature(block), details),
                                    ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _document_blocks(client: NotionClient, page_id: str) -> list[dict]:
    rows = client.list_children(page_id)
    for row in rows:
        if row.get("type") == "table" and row.get("has_children") and "children" not in row.get("table", {}):
            row["table"] = {**row["table"], "children": client.list_children(row["id"])}
    return rows


def _reconcile_document(client: NotionClient, page_id: str, blocks: list[dict],
                        state: dict, key: str) -> None:
    """Repair the remote document and keep every unowned/edited block.

The first paragraph is the fixed app marker. New source content is placed
immediately after it. Only unchanged blocks recorded by this writer may be
retired; legacy, student and other-device content stays below the current
source with a historical label. Digest equality never bypasses a remote read.
"""
    expected = [_document_signature(block) for block in blocks]
    existing = _document_blocks(client, page_id)
    signatures = [_document_signature(block) for block in existing]
    manifest_key = key + "|verified-blocks"
    if signatures[:len(expected)] == expected:
        return
    previous = state.get(manifest_key) or {}
    if existing and signatures[0] != expected[0]:
        raise ValueError("Notion 同步文档的顶部已被手动修改；已保留内容并停止写入。")
    if existing:
        owned = previous.get("owned_blocks") or {}
        fresh = blocks[1:]
        if any(block.get("id") not in owned for block in existing[1:]):
            fresh += markdown_to_blocks(_HISTORICAL_SOURCE_NOTE)
        client.append_blocks(page_id, fresh, after=existing[0]["id"], retry=False)
        current = _document_blocks(client, page_id)
        if [_document_signature(block) for block in current[:len(blocks)]] != expected:
            raise ValueError("Notion 同步文档写入后验证失败；未更新摘要。")
        for block in existing[1:]:
            if (block.get("type") not in {"child_page", "child_database"}
                    and owned.get(block.get("id")) == _document_signature(block)):
                client.archive_block(block["id"], retry=False)
    else:
        client.append_blocks(page_id, blocks, retry=False)
    current = _document_blocks(client, page_id)
    if [_document_signature(block) for block in current[:len(blocks)]] != expected:
        raise ValueError("Notion 同步文档写入后验证失败；未更新摘要。")
    state[manifest_key] = {"id": page_id, "owned_blocks": {block["id"]: _document_signature(block)
                                                         for block in current[:len(blocks)]}}


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
        if item.get("source") == "calendar":
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


def _source_status(section: str, course: dict, count: int,
                   now: datetime | None = None) -> str:
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
                  f"读取到 {sum(1 for item in course.get('assignments', []) if item.get('source') != 'calendar')} 项正式作业、"
                  f"{sum(1 for item in course.get('assignments', []) if item.get('source') == 'calendar')} 项未关联日历事项、"
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
    return status


def _sync_source_status(client: NotionClient, page_id: str,
                        section: str, course: dict, count: int,
                        now: datetime | None = None) -> None:
    status = _source_status(section, course, count, now)
    for block in client.list_children(page_id):
        if block.get("type") != "paragraph":
            continue
        rich = (block.get("paragraph") or {}).get("rich_text") or []
        previous = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                           for part in rich)
        if previous.startswith("教学网状态："):
            # An old, genuine observation remains correctly dated. Do not
            # rewrite unchanged official facts merely to advance that date;
            # the application separately reports this refresh's completion.
            # Counts, incomplete states and every deadline line still differ.
            def without_clock(text):
                matched = re.fullmatch(r'教学网状态：([^\n]+?) 读取到 (.+)', text, re.DOTALL)
                if matched is None:
                    return None
                clock, content = matched.groups()
                try:
                    if clock.endswith(' 北京时间'):
                        datetime.strptime(clock, '%Y-%m-%d %H:%M 北京时间')
                    else:
                        if datetime.fromisoformat(clock.replace('Z', '+00:00')).tzinfo is None:
                            return None
                except ValueError:
                    return None
                return content
            actual_content = without_clock(previous)
            if actual_content is not None and actual_content == without_clock(status):
                return
            if previous != status:
                client.update_paragraph(block["id"], status)
                expected = _source_block_signature(markdown_to_blocks(status)[0])
                if not any(_source_block_signature(row) == expected for row in client.list_children(page_id)):
                    raise ValueError("Notion 教学网状态写入后验证失败。")
            return
    client.append_blocks(page_id, markdown_to_blocks(status), retry=False)
    expected = _source_block_signature(markdown_to_blocks(status)[0])
    if not any(_source_block_signature(row) == expected for row in client.list_children(page_id)):
        raise ValueError("Notion 教学网状态写入后验证失败。")


def _source_read_time_text(block):
    if block.get('type') != 'paragraph':
        return None
    parts = block['paragraph'].get('rich_text') or []
    if (len(parts) != 1 or parts[0].get('type') != 'text'
            or (parts[0].get('text') or {}).get('link')):
        return None
    text = (parts[0].get('text') or {}).get('content') or parts[0].get('plain_text') or ''
    if not text.startswith('请以教学网原页面为准。目录读取：'):
        return None
    if _document_signature(block) != _document_signature(markdown_to_blocks(text)[0]):
        return None
    return text


def _source_body_matches(existing, desired):
    """Compare the displayed source, allowing only its observation time to age."""
    if len(existing) < len(desired):
        return False
    time_changes = 0
    for actual, wanted in zip(existing, desired):
        if _document_signature(actual) == _document_signature(wanted):
            continue
        old_text, new_text = _source_read_time_text(actual), _source_read_time_text(wanted)
        if (old_text is None or new_text is None
                or '。课程标识：' not in old_text
                or old_text.split('。课程标识：', 1)[-1] != new_text.split('。课程标识：', 1)[-1]):
            return False
        time_changes += 1
    return time_changes <= 1


def _recorded_source_matches(existing, owned, course_id):
    """Permit read-only reuse of one changed clock, without claiming its signature."""
    current = {block.get('id'): block for block in existing if block.get('id')}
    changed_times = 0
    for identifier, signature in owned.items():
        block = current.get(identifier)
        if block is None:
            return False
        if _source_block_signature(block) == signature:
            continue
        text = _source_read_time_text(block)
        if (text is None or text.count('。课程标识：') != 1
                or not text.endswith(f'。课程标识：{course_id}。')):
            return False
        changed_times += 1
    return changed_times <= 1


def _refresh_source_read_time(client, page_id, existing, desired, previous):
    """Refresh only an unchanged, recorded source card's observation time."""
    owned = previous.get("owned_blocks") or {}
    if (previous.get("id") != page_id or previous.get("layout") != 4
            or previous.get("pending") or not owned
            or any(_source_block_state(existing).get(key) != signature
                   for key, signature in owned.items())):
        return None
    if len(existing) < len(desired):
        return None
    changes = []
    for actual, wanted in zip(existing, desired):
        if actual.get("id") not in owned:
            return None
        if _document_signature(actual) == _document_signature(wanted):
            continue
        old_text, new_text = _source_read_time_text(actual), _source_read_time_text(wanted)
        if (old_text is None or new_text is None
                or '。课程标识：' not in old_text
                or old_text.split('。课程标识：', 1)[-1] != new_text.split('。课程标识：', 1)[-1]):
            return None
        changes.append((actual['id'], new_text, _source_block_signature(wanted)))
    if len(changes) > 1:
        return None
    for identifier, text, _ in changes:
        client.update_paragraph(identifier, text)
    if changes:
        actual = client.list_children(page_id)
        expected = {**_source_block_state(existing),
                    **{identifier: signature for identifier, _, signature in changes}}
        if ([row.get('id') for row in actual] != [row.get('id') for row in existing]
                or _source_block_state(actual) != expected):
            raise ValueError('Notion 卡片读取时间更新后内容发生变化；原记录保留，未确认完成。')
    return {**owned, **{identifier: signature for identifier, _, signature in changes}}


@verified_workspace_operation('source-section')
def _sync_source_section(client: NotionClient, data_dir: Path, course_id: str,
                         section: str, page_id: str, course: dict, state: dict,
                         tick=None, *, state_path: Path | None = None,
                         recovery_state: dict | None = None) -> None:
    children = _indexed_children(client, page_id)
    items = _source_items(course, section)
    title_counts = Counter(str(item.get("title") or "未命名条目")[:86] for item in items)
    def item_title(item):
        base = str(item.get('title') or '未命名条目')[:86]
        key = _source_key(course_id, section, item)
        return (base + ' · ' + hashlib.sha256(key.encode('utf8')).hexdigest()[:8]
                if title_counts[base] > 1 else base)
    existing_ids = list(dict.fromkeys(children[item_title(item)]['id']
                        for item in items if item_title(item) in children))
    # Finish every complete read before changing either cards or local state.
    # Workers only read independent pages; recovery and writes stay serial.
    observed = {}
    if existing_ids:
        with ThreadPoolExecutor(max_workers=min(4, len(existing_ids))) as pool:
            observed = dict(zip(existing_ids, pool.map(client.list_children, existing_ids)))
    unchanged = {}
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
        # Older workspace versions wrote their interim receipts into the
        # legacy journal. Adopt only an exact child identity independently
        # listed under this workspace's verified center; foreign pages never
        # become ownership evidence here. The block checks below still apply.
        if not previous and existing and isinstance(recovery_state, dict):
            candidate = recovery_state.get(key)
            if isinstance(candidate, dict) and candidate.get('id') == existing['id']:
                previous = candidate
        blocks = _item_blocks(course_id, section, item,
                              course.get("synced_at_label") or course.get("synced_at", ""))
        if existing is None:
            created = client.create_page(page_id, title, children=blocks, retry=False)
            children[title] = {"id": created["id"], "title": title}
            page_id_for_item = created["id"]
            readback = client.list_children(page_id_for_item)
            if [_source_block_signature(row) for row in readback] != [_source_block_signature(row) for row in blocks]:
                raise ValueError(f"Notion 卡片「{title}」创建后验证失败；未更新本地摘要。")
            owned = _source_block_state(readback)
            legacy = False
        else:
            page_id_for_item = existing["id"]
            existing_blocks = observed[page_id_for_item]
            # Read-only reuse never authorizes mutation with an earlier batch
            # observation. Changed/recovering cards receive a new full read
            # immediately before the original ownership and write guards.
            read_only = (previous.get('id') == page_id_for_item
                         and previous.get('layout') == 4 and not previous.get('pending')
                         and previous.get('hash') == digest and previous.get('owned_blocks')
                         and _recorded_source_matches(existing_blocks, previous['owned_blocks'], course_id)
                         and _source_body_matches(existing_blocks, blocks))
            if not read_only:
                existing_blocks = client.list_children(page_id_for_item)
            first = existing_blocks[0] if existing_blocks else {}
            rich = (first.get(first.get("type") or "", {}) or {}).get("rich_text") or []
            marker = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                             for part in rich)
            owned_before = previous.get("owned_blocks") or {}
            if previous.get("id") != page_id_for_item and item.get("source_id"):
                from urllib.parse import quote
                ownership_url = ("https://course.pku.edu.cn/#pku-card/" +
                                 quote(key, safe="") + "/" + _source_block_signature(first))
                remote_owned = any(
                    ((part.get("text") or {}).get("link") or {}).get("url") == ownership_url
                    for row in existing_blocks
                    for part in (row.get(row.get("type", "")) or {}).get("rich_text", [])
                )
                if remote_owned:
                    previous = {"id": page_id_for_item, "layout": 4}
                    owned_before = {first["id"]: _source_block_signature(first)}
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

            if (previous.get("id") == page_id_for_item and previous.get("layout") == 4
                    and not pending and owned_before
                    and _recorded_source_matches(existing_blocks, owned_before, course_id)
                    and _source_body_matches(existing_blocks, blocks)):
                # The fresh displayed body, not the digest alone, is authority.
                # Keep an unchanged source untouched; another writer's exact
                # current body may be read, but its IDs never become ours.
                refreshed = None
                if previous.get('hash') != digest:
                    refreshed = _refresh_source_read_time(
                        client, page_id_for_item, existing_blocks, blocks, previous)
                state[key] = {**previous, 'hash': digest,
                              'owned_blocks': refreshed if refreshed is not None else owned_before}
                if read_only:
                    unchanged[page_id_for_item] = existing_blocks
                if data_dir is not None:
                    _save_source_state(data_dir, state, path=state_path)
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
                _save_source_state(data_dir, state, path=state_path)
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
                _save_source_state(data_dir, state, path=state_path)
            client.update_paragraph(first["id"], _summary_line(section, item))
            for block_id in to_archive:
                client.archive_block(block_id, retry=False)
            owned = {first["id"]: _source_block_signature(blocks[0])}
            owned.update(_source_block_state(added))
        verified = _source_block_state(client.list_children(page_id_for_item))
        if not owned or any(verified.get(identifier) != signature for identifier, signature in owned.items()):
            raise ValueError(f"Notion 卡片「{title}」写入后验证失败；未更新本地摘要。")
        state[key] = {"id": page_id_for_item, "hash": digest, "layout": 4,
                      "owned_blocks": owned, "legacy": legacy}
        if data_dir is not None:
            _save_source_state(data_dir, state, path=state_path)
    if unchanged:
        identifiers = list(unchanged)
        with ThreadPoolExecutor(max_workers=min(4, len(identifiers))) as pool:
            final = list(pool.map(client.list_children, identifiers))
        for identifier, actual in zip(identifiers, final):
            before = unchanged[identifier]
            if ([row.get('id') for row in actual] != [row.get('id') for row in before]
                    or [_document_signature(row) for row in actual] !=
                       [_document_signature(row) for row in before]):
                raise ValueError('Notion 来源卡片在同步期间发生变化；原内容保留，未确认完成。')
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
    _reconcile_document(client, page["id"], blocks, state, key)
    state[key] = url


def _sync_lecture_placeholder(client: NotionClient, lecture_id: str) -> None:
    if not client.list_children(lecture_id):
        blocks = markdown_to_blocks(LECTURE_PLACEHOLDER)
        client.append_blocks(lecture_id, blocks, retry=False)
        if [_source_block_signature(row) for row in client.list_children(lecture_id)] != [_source_block_signature(row) for row in blocks]:
            raise ValueError("Notion 讲次页创建后验证失败。")


@verified_workspace_operation('lecture')
def _sync_source_lectures(client, parent_id, lessons, titles, children, *, tick=None,
                          final_observation=None):
    # One fresh identity snapshot for this batch, never a persisted cache.
    # Reread the complete parent after the batch as well: student edits or a
    # concurrently introduced duplicate cannot become a verified result.
    observed = _lecture_observations(client, children)
    _verify_lecture_identities(lessons, observed)
    result, unchanged = {}, {}
    for lesson in lessons:
        base = titles[lesson['id']]
        stable = _unchanged_source_lecture(lesson, base, children, observed)
        if stable is not None:
            page, notes = stable
            made = False
            children[base] = page
            unchanged[page['id']] = (page['title'], notes)
        else:
            page, made = _ensure_source_lecture(
                client, parent_id, lesson, base, children, observed=observed)
        result[lesson['id']] = (page, made)
        if tick:
            tick(lesson)
    final = _lecture_observations(client, _indexed_children(client, parent_id))
    _verify_lecture_identities(lessons, final, result=result)
    for identifier, (title, notes) in unchanged.items():
        page, rows = final[identifier]
        if page.get('title') != title or _lecture_note_identity(rows) != notes:
            raise ValueError('Notion 讲次标题或笔记子页在同步期间发生变化；请核对后重试，已有记录已保留。')
    if final_observation is not None:
        from copy import deepcopy
        final_observation.update(deepcopy(final))
    return result


def _lecture_note_identity(rows):
    # list_child_pages derives these same child_page blocks from a complete
    # paginated list_children response. Never use a persisted note cache.
    return frozenset((row.get('id'), (row.get('child_page') or {}).get('title', ''))
                     for row in rows if row.get('type') == 'child_page'
                     and (row.get('child_page') or {}).get('title', '').startswith(
                         ('转写笔记 ·', '更新笔记 ·')))


def _unchanged_source_lecture(lesson, base, children, observed):
    marker = _lecture_marker(lesson)
    if marker is None:
        return None
    signature = _source_block_signature(marker)
    matches = [(page, rows) for page, rows in observed.values()
               if any(_source_block_signature(row) == signature for row in rows)]
    if len(matches) != 1:
        return None
    page, rows = matches[0]
    notes = _lecture_note_identity(rows)
    preferred = _find_lecture(children, base)
    if (page.get('title') != _lecture_status_title(base, bool(notes))
            or (preferred is not None and preferred['id'] != page['id'])):
        return None
    # Nothing needs writing. The complete final parent/body observations must
    # still prove the same unique source, title and note children before the
    # verified operation can complete. Changed/new pages keep the write path.
    return page, notes


def _lecture_observations(client, children):
    pages = [page for page in {page['id']: page for page in children.values()}.values()
             if page.get('title') not in SECTION_TITLES]
    if not pages:
        return {}
    def read(page):
        return page['id'], (page, client.list_children(page['id']))
    # Independent full reads may overlap; mutations and durable intentions
    # remain on the calling thread. Every read must complete successfully.
    with ThreadPoolExecutor(max_workers=min(4, len(pages))) as pool:
        return dict(pool.map(read, pages))


def _lecture_marker(lesson):
    from urllib.parse import quote
    identity = lesson.get('source_id')
    return (_linked_paragraph('查看教学网讲次来源', 'https://course.pku.edu.cn/' +
            'webapps/bb-streammedia-hqy-BBLEARN/videoList.action#' + quote(identity, safe=''))
            if identity else None)


def _verify_lecture_identities(lessons, observed, *, result=None):
    for lesson in lessons:
        marker = _lecture_marker(lesson)
        if marker is None:
            continue
        signature = _source_block_signature(marker)
        matches = [page for page, rows in observed.values()
                   if any(_source_block_signature(row) == signature for row in rows)]
        if len(matches) > 1:
            raise ValueError('Notion 中有多个相同教学网讲次标识；已停止自动写入。')
        if result is not None and (len(matches) != 1 or
                matches[0]['id'] != result[lesson['id']][0]['id']):
            raise ValueError('Notion 教学网讲次标识在同步期间发生变化；未确认目录完成。')


def _ensure_source_lecture(client: NotionClient, parent_id: str, lesson: dict,
                           base: str, children: dict[str, dict], *, observed=None) -> tuple[dict, bool]:
    marker = _lecture_marker(lesson)
    if marker:
        signature = _source_block_signature(marker)
        matches = []
        sources = observed if observed is not None else _lecture_observations(client, children)
        for page, rows in sources.values():
            if any(_source_block_signature(row) == signature for row in rows):
                matches.append(page)
        if len(matches) > 1:
            raise ValueError("Notion 中有多个相同教学网讲次标识；已停止自动写入。")
        if matches:
            if observed is not None and not any(_source_block_signature(row) == signature
                    for row in client.list_children(matches[0]['id'])):
                raise ValueError('Notion 教学网讲次标识在同步期间发生变化；未覆盖页面。')
            children[base] = matches[0]
    made_with_body = False
    if _find_lecture(children, base) is None:
        # The placeholder and stable source marker belong to the creation
        # itself. Avoid two later append mutations for every empty lesson.
        title = _lecture_status_title(base, False)
        body = markdown_to_blocks(LECTURE_PLACEHOLDER)
        if marker:
            body.append(marker)
        created = client.create_page(parent_id, title, children=body, retry=False)
        children[title] = {'id': created['id'], 'title': title}
        made_with_body = True
    page, made = _ensure_status_lecture(client, parent_id, base, children)
    made = made or made_with_body
    _sync_lecture_placeholder(client, page["id"])
    if marker:
        signatures = {_source_block_signature(row) for row in client.list_children(page["id"])}
        if signature not in signatures:
            client.append_blocks(page["id"], [marker], retry=False)
        verified = client.list_children(page["id"])
        if not any(_source_block_signature(row) == signature for row in verified):
            raise ValueError("Notion 教学网讲次标识写入后验证失败。")
        if observed is not None:
            observed[page['id']] = (page, verified)
    return page, made


def _recording_note_snapshot(client, lessons, titles, lecture_children):
    pages = {lecture_children[titles[lesson['id']]]['id'] for lesson in lessons}
    if not pages:
        return {}
    def read(identifier):
        return identifier, frozenset((row['id'], row.get('title', ''))
            for row in client.list_child_pages(identifier)
            if row.get('title', '').startswith(('转写笔记 ·', '更新笔记 ·')))
    with ThreadPoolExecutor(max_workers=min(4, len(pages))) as pool:
        return dict(pool.map(read, sorted(pages)))


def _sync_recording_section(client: NotionClient, page_id: str,
                            lessons: list[dict], titles: dict[str, str],
                            lecture_children: dict[str, dict], state: dict,
                            course_id: str, *, observed_notes=None) -> None:
    rows = []
    if observed_notes is None:
        observed_notes = _recording_note_snapshot(client, lessons, titles, lecture_children)
    else:
        # Only consume the exact lessons in this immediately adjacent handoff;
        # the source-lecture read may also contain unrelated student pages.
        identifiers = {lecture_children[titles[lesson['id']]]['id'] for lesson in lessons}
        if not identifiers <= observed_notes.keys():
            raise ValueError('Notion 讲次笔记完整观察缺失；未确认课堂记录。')
        observed_notes = {identifier: observed_notes[identifier] for identifier in identifiers}
    for lesson in lessons:
        page = lecture_children[titles[lesson["id"]]]
        has_note = bool(observed_notes[page['id']])
        status = "有转写笔记，待质量验收" if has_note else "待整理，仅有目录"
        rows.append((titles[lesson["id"]], page["id"], status))
    rows.sort(key=lambda row: (row[2] != "有转写笔记，待质量验收", row[0]))
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()
    key = course_id + "|课堂记录"
    blocks = markdown_to_blocks(
        "课堂页按教学网录像目录建立。待整理表示没有笔记；有转写笔记仍需核对整节内容、课件、老师要求和正式作业，不能仅凭页面存在判断已验收。"
    )
    for title, lecture_id, status in rows:
        blocks.append(_linked_paragraph(f"{title} · {status}",
                                        "https://www.notion.so/" + lecture_id.replace("-", "")))
    existing = client.list_children(page_id)
    # Notion exposes nested lecture pages as child_page blocks in this list.
    # Only ignore the exact pages already resolved for these official lessons;
    # unknown student pages and prose keep the existing adoption protection.
    lecture_ids = {lecture_id for _, lecture_id, _ in rows}
    index_content = [block for block in existing
                     if not (block.get("type") == "child_page" and block.get("id") in lecture_ids)]
    if index_content:
        first = index_content[0]
        rich = (first.get(first.get("type") or "", {}) or {}).get("rich_text") or []
        marker = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                         for part in rich)
        if not marker.startswith("课堂页按教学网录像目录建立"):
            raise ValueError("课堂记录索引含有手动内容，已停止自动覆盖。")
    # A local digest is only a hint. Another device can have changed Notion.
    # Add missing rows, retaining unseen lectures and student edits. Never
    # replace this shared index with the contents of one device's cache.
    signatures = {_source_block_signature(block) for block in existing}
    missing = [block for block in blocks
               if _source_block_signature(block) not in signatures]
    if missing:
        client.append_blocks(page_id, missing, retry=False)
    verified = {_source_block_signature(block) for block in client.list_children(page_id)}
    if any(_source_block_signature(block) not in verified for block in blocks):
        raise ValueError("Notion 课堂记录写入后验证失败；未更新本地同步摘要。")
    if _recording_note_snapshot(client, lessons, titles, lecture_children) != observed_notes:
        raise ValueError('Notion 讲次笔记在课堂记录同步期间发生变化；请核对后重试，已有内容已保留。')
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
    blocks = markdown_to_blocks(markdown)
    # Keep the existing append path for oversized documents. A small new
    # document needs one tracked write instead of empty-create then append.
    initial = blocks if len(blocks) <= 100 else None
    page, made = _ensure_indexed_child(client, course_page_id, ASSESSMENT_TITLE,
                                      children, initial_blocks=initial)
    existing = _document_blocks(client, page["id"])
    if made and initial:
        if [_document_signature(row) for row in existing] != [_document_signature(row) for row in blocks]:
            raise ValueError("Notion 考核评测页创建后验证失败；未记录内容所有权。")
        state[key + '|verified-blocks'] = {'id': page['id'], 'owned_blocks': {
            row['id']: _document_signature(row) for row in existing}}
    if existing:
        first = existing[0]
        rich = (first.get(first.get("type") or "", {}) or {}).get("rich_text") or []
        marker = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                         for part in rich)
        if not marker.startswith(INTRO[:24]):
            raise ValueError("考核评测页含有手动内容，已停止自动覆盖。")
    _reconcile_document(client, page["id"], blocks, state, key)
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


def _dashboard_blocks(data_dir: Path, titles: dict[str, str], state: dict, course_ids=None) -> list[dict]:
    from datetime import timedelta
    from .campus_todos import announcement_source_url
    now = datetime.now(timezone.utc)
    due_rows = []
    notice_rows = []
    handbook_rows = []
    course_rows = campus_courses(data_dir)["courses"]
    if course_ids is not None:
        course_rows = [row for row in course_rows if row["id"] in course_ids]
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
        kind = ("公告待办" if item.get("source") == "announcement" else
                "教学网日历事项（目标关联待核对，所列为日历时间）" if item.get("source") == "calendar" else "正式作业")
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
                    titles: dict[str, str], state: dict, course_ids=None) -> None:
    blocks = _dashboard_blocks(data_dir, titles, state, course_ids)
    digest = hashlib.sha256(json.dumps(blocks, ensure_ascii=False, sort_keys=True)
                            .encode("utf-8")).hexdigest()
    page = _child(client, home_id, DASHBOARD_TITLE)
    existing = client.list_children(page["id"])
    if existing:
        first = existing[0]
        rich = (first.get(first.get("type") or "", {}) or {}).get("rich_text") or []
        marker = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                         for part in rich)
        if marker != DASHBOARD_MARKER:
            raise ValueError("近期待办页面已有手动内容；未自动覆盖。")
    _reconcile_document(client, page["id"], blocks, state, "|dashboard")
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

@contextmanager
def _catalog_authority(data_dir: Path, settings, course_ids=None, *, verified_steps=False,
                       tracked_mutations=False, check_cancel=None, before_existing_projection=None):
    from .. import catalog_snapshots as catalog
    if settings is None:
        raise catalog.CatalogUnavailable("共享目录必须使用账号级教学网快照；本地缓存保持只读。")
    discovering = course_ids is None
    if discovering:
        course_ids = catalog.projection_readable_course_ids(
            data_dir, sorted(row["id"] for row in campus_courses(data_dir)["courses"]))
        catalog.activate_account(settings)
    else:
        catalog.activate_account(settings)
    for course_id in course_ids:
        catalog.assert_portal_readable(data_dir, course_id)
    if not course_ids:
        raise catalog.CatalogUnavailable("尚无已验证的教学网课程快照。")
    read = catalog.published_current if discovering else catalog.current
    envelopes = [envelope for envelope in catalog.read_current_batch(
        settings, course_ids, read=read, check_cancel=check_cancel) if envelope is not None]
    if not envelopes:
        raise catalog.CatalogUnavailable("尚无已验证的教学网课程快照。")
    def join_before_existing_receipt(envelope):
        if before_existing_projection is not None:
            # Recovery can issue Notion reads. Wait before the original fresh
            # identity GETs start their short, unchanged prepared-read TTL.
            from ..catalog_schema import fingerprint
            filename = fingerprint({'course': envelope['snapshot']['course_id']}) + '.json'
            if any((data_dir / '.projection-receipts').glob('*/' + filename)):
                before_existing_projection()
    for envelope in envelopes:
        join_before_existing_receipt(envelope)
    from ..projection_receipts import PreparedReadExpired, prepared_cohort, prepared_read_phase
    with ExitStack() as stack:
        from .. import platform
        stack.enter_context(platform.request_scope())
        binding = catalog._read_binding(settings)
        for prefetch in (True, False):
            try:
                with ExitStack() as admission:
                    cohort = admission.enter_context(prepared_cohort(
                        verified_steps=verified_steps, tracked_mutations=tracked_mutations))
                    if prefetch:
                        admission.enter_context(prepared_read_phase(settings, envelopes, check_cancel=check_cancel))
                    for envelope in envelopes:
                        if check_cancel:
                            check_cancel()
                        if catalog._read_binding(settings) != binding:
                            raise catalog.CatalogUnavailable('账号或连接已变化，请重新读取课程。')
                        join_before_existing_receipt(envelope)
                        admission.enter_context(catalog.projection(settings, envelope))
                    stack.enter_context(admission.pop_all())
                break
            except PreparedReadExpired:
                if not prefetch:
                    raise
                # The failed admission has released every known permit before
                # this new phase. Discard all prefetched evidence and perform
                # the original fresh serial checks. Never extend the TTL, retry
                # a changed snapshot, or replay the writer body below.
        for envelope in envelopes:
            catalog.save_cache(data_dir, envelope)
        cohort.begin()
        if check_cancel:
            check_cancel()
        yield frozenset(envelope["snapshot"]["course_id"] for envelope in envelopes)
        cohort.complete()


def sync_catalog(data_dir: Path, token: str, stage=None, progress=None,
                 item_progress=None, *, settings=None, course_id: str = "",
                 pending_read_plan=None, operation_nonce=None) -> dict:
    from .notion_workspace import saved_workspace, sync_workspace
    if saved_workspace(data_dir):
        if stage:
            stage("同步并验证新版 Notion 目录")
        options = {'course_id': course_id} if course_id else {}
        if pending_read_plan is not None:
            options.update(pending_read_plan=pending_read_plan, operation_nonce=operation_nonce)
        if stage:
            options['check_cancel'] = lambda: stage('读取并验证新版 Notion 课程目录')
        result = sync_workspace(settings, **options)
        if progress:
            progress(len(result["courses"]), len(result["courses"]), "courses", 0)
        return result
    scope = {"course_ids": [course_id]} if course_id else {}
    with _catalog_authority(data_dir, settings, tracked_mutations=True, **scope) as course_ids:
        projection_scope = {"update_dashboard": False} if course_id else {}
        return _sync_catalog_projection(data_dir, token, stage, progress, item_progress,
                                        course_ids=course_ids, **projection_scope)


def repair_classroom_index(settings, course_id: str) -> dict:
    """Repair one existing classroom index from the current account snapshot.

This is the incident-repair path: retain the home, course, existing lecture
pages and every student block. No dashboard, other course or assessment page
is rewritten. Requires a confirmed complete portal refresh on this device.
"""
    from .. import catalog_snapshots as catalog
    root = Path(settings.data_dir)
    catalog.activate_account(settings)
    catalog.assert_portal_readable(root, course_id)
    expected = catalog.cached(root, course_id)
    if expected is None:
        raise catalog.CatalogUnavailable("没有当前账号已确认的教学网快照，禁止修复共享目录。")
    home = legacy_home(root)
    if not home:
        raise ValueError("Windows 尚未配置既有 Notion 学习主页。")
    title = _course_titles(root).get(course_id)
    state = _source_state(root)
    with catalog.projection(settings, expected):
        with NotionClient(settings.notion_token) as client:
            courses = _indexed_children(client, home["id"])
            if title not in courses:
                raise ValueError("既有 Notion 主页中没有唯一对应课程页；未创建或覆盖页面。")
            course_page = courses[title]
            sections = _indexed_children(client, course_page["id"])
            if "课堂记录" not in sections:
                raise ValueError("既有课程没有课堂记录索引；未创建或覆盖页面。")
            page_id = sections["课堂记录"]["id"]
            before = len(client.list_children(page_id))
            detail = campus_course(root, course_id)
            titles = _lecture_titles(detail)
            children = _indexed_children(client, course_page["id"])
            _sync_source_lectures(client, course_page['id'], detail['lessons'], titles, children)
            _sync_recording_section(client, page_id, detail["lessons"], titles, children, state, course_id)
            after = len(client.list_children(page_id))
        _save_source_state(root, state)
    return {"course_id": course_id, "revision": expected["revision"],
            "lectures_verified": len(detail["lessons"]), "blocks_added": after - before,
            "index_url": "https://www.notion.so/" + page_id.replace("-", "")}


def _sync_catalog_projection(data_dir: Path, token: str, stage=None, progress=None,
                             item_progress=None, *, course_ids=None,
                             update_dashboard: bool = True) -> dict:
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
    if course_ids is not None:
        rows = [row for row in rows if row["id"] in course_ids]
    if not update_dashboard and len(rows) != 1:
        raise ValueError("这门课程的教学网目录未唯一确认；未执行 Notion 同步。")
    titles = _course_titles(data_dir)
    created_courses = created_lectures = recovered_notes = 0
    total_items = catalog_plan_total(rows) - int(not update_dashboard)
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
                initial = (markdown_to_blocks(_source_status(
                    section, detail, len(_source_items(detail, section))))
                    if section in {'通知', '作业', '资料'} else None)
                section_page, _ = _ensure_indexed_child(
                    client, course_page["id"], section, course_children,
                    initial_blocks=initial,
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
            def lecture_tick(lesson):
                tick(f"{row['title']} · 讲次页："
                     + " · ".join(part for part in (lesson["date"], lesson["title"]) if part))
            verified_lectures = _sync_source_lectures(
                client, course_page['id'], detail['lessons'], lecture_titles,
                lecture_children, tick=lecture_tick)
            created_lectures += sum(int(made) for _, made in verified_lectures.values())
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
        if update_dashboard:
            _sync_dashboard(client, data_dir, home["id"], titles, source_state, course_ids)
            _save_source_state(data_dir, source_state)
            tick("汇总近期待办与新通知")
    return {"courses": len(rows), "created_courses": created_courses,
            "created_lectures": created_lectures, "recovered_notes": recovered_notes, "home_url": home.get("url", "")}


def ensure_recording_target(data_dir: Path, token: str, job, *, settings=None) -> str:
    from .notion_workspace import saved_workspace, sync_workspace
    if saved_workspace(data_dir):
        if settings is None:
            raise ValueError("新版讲次目标需要已验证的账号快照。")
        from .recordings_api import recording_id
        workspace = sync_workspace(settings, course_id=job.recording.course_id)
        return workspace["courses"][job.recording.course_id]["lectures"][recording_id(job)]["id"]
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

