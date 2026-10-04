"""Explicit, one-recording-at-a-time desktop workflow for lecture notes."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..envfile import write_env_values
from ..notion_login import env_file_path
from ..pipeline import RecordingJob, collect_jobs, download_job, process_job, recording_stage
from ..store import safe_name

logger = logging.getLogger(__name__)


class CampusCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=300)


class OrganizeRequest(BaseModel):
    course_id: str
    lecture_id: str
    direct_oss: bool = False
    regenerate: bool = False


class CampusProcessRequest(BaseModel):
    direct_oss: bool = False
    regenerate: bool = False
    reuse_existing: bool = False


class CourseSourceRequest(BaseModel):
    campus_course_id: str

class HomeRequest(BaseModel):
    parent_page_id: str


class MaterialMatchRequest(BaseModel):
    lecture_id: str = ""
    ignore: bool = False


class ExistingNotionLink(BaseModel):
    url: str


class TermReviewConfirmation(BaseModel):
    segment_index: int
    transcript_sha256: str
    confirmed_spoken_term: str
    confirmed: bool = False


@dataclass
class Work:
    kind: str
    recording_id: str = ""
    state: str = "running"
    stage: str = "准备中"
    error: str = ""
    result_url: str = ""
    lecture_url: str = ""
    progress_done: int = 0
    progress_total: int = 0
    progress_remaining: int = 0
    progress_unit: str = ""
    progress_percent: int = 0
    stage_index: int = 0
    stage_total: int = 0
    current_item: str = ""
    bytes_done: int = 0
    speed_bps: int = 0
    notion_synced: bool = False
    summary_created: int = 0
    summary_existing: int = 0
    result_urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failure_id: str = ""

    def as_dict(self) -> dict:
        return vars(self).copy()


def _unexpected_process_failure(stage: str) -> str:
    """Name the failed product step without exposing exception details."""
    stage = stage.strip() or "准备任务"
    if "Notion" in stage or "发布" in stage:
        return "写入 Notion 时中断；本机已有转写和笔记会保留，请检查 Notion 连接后重试。"
    if any(name in stage for name in ("教学网", "目录", "下载录像", "获取录像", "连接")):
        return "读取教学网或录像时中断；已下载的本地文件会保留，请检查网络后重试。"
    if any(name in stage for name in ("转写", "音轨", "音频")):
        return "音频处理或转写时中断；已完成的本地结果会保留，请检查平台登录与额度后重试。"
    if any(name in stage for name in ("笔记", "AI")):
        return "生成课堂笔记时中断；本地转写会保留，请检查平台额度后重试。"
    if any(name in stage for name in ("课堂画面", "关键帧", "补图")):
        return "提取课堂画面时中断；文字转写和笔记会保留，请重试补充课堂画面。"
    return f"任务在“{stage}”阶段中断；已生成的本地文件会保留，请稍后重试。"


def recording_id(job: RecordingJob) -> str:
    identity = "\n".join((job.recording.course_id, job.recording.recorded_at,
                          job.recording.title, job.course_dir.name))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def find_job(data_dir: Path, key: str) -> RecordingJob | None:
    return next((job for job in collect_jobs(data_dir) if recording_id(job) == key), None)


def _saved_keyframes(directory: Path) -> list[dict]:
    """Frames already extracted for this recording, if any.

    Regenerating a note from an existing transcript should still get the
    slide-page evidence those frames carry, even when the video is long gone.
    """
    try:
        payload = json.loads((directory / "keyframes" / "index.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    rows = payload.get("keyframes")
    return rows if isinstance(rows, list) else []


def list_recordings(data_dir: Path) -> list[dict]:
    rows = []
    for job in collect_jobs(data_dir):
        stage, unavailable = recording_stage(job)
        rows.append({
            "id": recording_id(job), "course": job.course_name,
            "title": job.recording.title, "date": job.recording.date,
            "stage": stage, "unavailable_reason": unavailable,
            "video_available": job.video.exists(),
        })
    return rows


def _safe_attachment_path(url: str) -> str:
    """Keep only stable campus WebDAV paths; discard signed or external URLs."""
    if not isinstance(url, str) or "\\" in url:
        return ""
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or parts.query or parts.fragment:
        return ""
    if not parts.path.startswith("/bbcswebdav/") or ".." in parts.path.split("/"):
        return ""
    return parts.path


def sync_recording_index(settings, stage: Callable[[str], None], progress=None,
                         course_id: str = "") -> None:
    """Read the campus index into the local data directory.

    ``course_id`` limits the refresh to one course, so a stale snapshot can be
    renewed in seconds before a note is published instead of re-reading every
    course the student has.
    """
    from ..auth import get_session
    from ..discover import discover_courses, select_courses
    from ..recordings import list_recordings as fetch_recordings
    from ..materials import (ToolUnavailable, fetch_announcements, fetch_deadlines,
                             merge_deadlines, walk_materials)
    from ..store import safe_name
    from ..models import Assignment

    if not settings.pku_username or not settings.pku_password:
        raise ValueError("请先填写教学网账号和密码。")
    stage("登录教学网")
    client = get_session(username=settings.pku_username, password=settings.pku_password)
    try:
        courses = list(select_courses(discover_courses(client), settings))
        if course_id:
            selected = [course for course in courses if course.course_id == course_id]
            if not selected:
                raise ValueError("教学网当前学期课程中没有这门课，请重新登录后更新课程目录。")
            courses = selected
        elif not courses:
            raise ValueError("教学网没有找到当前学期课程。")
        root = Path(settings.data_dir)
        try:
            deadlines_by_course = fetch_deadlines(client)
            calendar_available = True
        except Exception:
            logger.exception("campus calendar deadlines unavailable")
            deadlines_by_course = {}
            calendar_available = False
        if progress:
            progress(0, len(courses), "courses", 0)
        for course_number, course in enumerate(courses, 1):
            stage(f"同步录音索引：{course.name}")
            if progress:
                progress(course_number - 1, len(courses), "courses", 0)
            recordings = fetch_recordings(client, course.course_id)
            stage(f"读取课件目录：{course.name}")
            if progress:
                progress(course_number - 1, len(courses), "courses", 0)
            try:
                material_items, tree_assignments = walk_materials(client, course.course_id)
            except Exception:
                logger.exception("campus material index unavailable for %s", course.course_id)
                material_items, tree_assignments = None, []
            try:
                notices = fetch_announcements(client, course.course_id)
                notice_status = "available"
            except ToolUnavailable:
                notices, notice_status = None, "unavailable"
            except Exception:
                logger.exception("campus announcements unavailable for %s", course.course_id)
                notices, notice_status = None, "error"
            course_dir = root / safe_name(course.directory, course.course_id.strip("_"))
            assignment_dir = course_dir / "assignments"
            previous_assignments = []
            if material_items is None or not calendar_available:
                try:
                    previous_assignments = [
                        Assignment.model_validate({"course_id": course.course_id,
                                                   **{key: value for key, value in item.items()
                                                      if key != "files"}})
                        for item in json.loads((assignment_dir / "index.json").read_text("utf-8"))
                    ]
                except (OSError, ValueError):
                    previous_assignments = []
            if material_items is None:
                tree_assignments = [item for item in previous_assignments
                                    if item.source != "calendar"]
            deadlines = (deadlines_by_course.get(course.course_id, []) if calendar_available
                         else [item for item in previous_assignments
                               if item.source == "calendar"])
            assignments = merge_deadlines(tree_assignments, deadlines)
            index_dir = course_dir / "recordings"
            index_dir.mkdir(parents=True, exist_ok=True)
            previous_meta_path = course_dir / "course.json"
            try:
                previous_meta = json.loads(previous_meta_path.read_text("utf-8"))
            except (OSError, ValueError):
                previous_meta = {}
            meta = {**course.model_dump(), "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "announcements_status": notice_status,
                    "assignments_status": ("available" if material_items is not None and calendar_available
                                           else "partial" if material_items is not None or calendar_available
                                           else "error"),
                    "existing_notion_url": previous_meta.get("existing_notion_url", "")}
            if notices is not None:
                notice_dir = course_dir / "announcements"
                notice_dir.mkdir(parents=True, exist_ok=True)
                notice_rows = [{
                    "id": item.announcement_id, "title": item.title,
                    "posted_at": item.posted_at, "author": item.author,
                    "body_text": item.body_text,
                    "files": [asset.filename for asset in item.attachments],
                } for item in notices]
                temporary = notice_dir / "index.json.tmp"
                temporary.write_text(json.dumps(notice_rows, ensure_ascii=False, indent=1), "utf-8")
                temporary.replace(notice_dir / "index.json")
            if material_items is not None or calendar_available:
                assignment_dir.mkdir(parents=True, exist_ok=True)
                assignment_rows = [{
                    "title": item.title, "content_id": item.content_id,
                    "due_at": item.due_at, "source": item.source,
                    "instructions": item.instructions,
                    "files": [asset.filename for asset in item.attachments],
                    "source_links": [link.model_dump() for link in item.source_links],
                } for item in assignments]
                temporary = assignment_dir / "index.json.tmp"
                temporary.write_text(json.dumps(assignment_rows, ensure_ascii=False, indent=1), "utf-8")
                temporary.replace(assignment_dir / "index.json")
            if material_items is not None:
                material_dir = course_dir / "materials"
                material_dir.mkdir(parents=True, exist_ok=True)
                material_rows = [{
                    "title": item.title, "kind": item.kind, "path": item.path,
                    "content_id": item.content_id, "body_text": item.body_text,
                    "parent_content_id": item.parent_content_id,
                    "files": [asset.filename for asset in item.attachments],
                    "source_links": [link.model_dump() for link in item.source_links],
                    "attachments": [{"filename": asset.filename, "path": path}
                                    for asset in item.attachments
                                    if (path := _safe_attachment_path(asset.url))],
                } for item in material_items]
                temporary = material_dir / "index.json.tmp"
                temporary.write_text(json.dumps(material_rows, ensure_ascii=False, indent=1), "utf-8")
                temporary.replace(material_dir / "index.json")
            for destination, payload in (
                (course_dir / "course.json", meta),
                (index_dir / "index.json", [item.model_dump() for item in recordings]),
            ):
                temporary = destination.with_name(destination.name + ".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
                temporary.replace(destination)
            if progress:
                progress(course_number, len(courses), "courses", 0)
    finally:
        client.close()


def _target_lecture(directory_service, course_id: str, lecture_id: str) -> dict:
    try:
        directory = directory_service.ensure_loaded()
    except Exception as exc:
        raise ValueError("无法读取 Notion 讲次目录，请先重新连接或同步。") from exc
    for lecture in directory.lectures_by_course.get(course_id, []):
        if lecture.id == lecture_id and lecture.url:
            return {"id": lecture.id}
    raise ValueError("请选择已就绪且属于该课程的 Notion 讲次页。")

def _note_format_issues(blocks: list[dict]) -> list[str]:
    """Reject leaked Markdown syntax in a page the app claims was published."""
    from ..notion import _INLINE_RE

    issues = []
    for block in blocks:
        kind = block.get("type")
        body = block.get(kind) or {}
        if body.get("children"):
            issues.extend(_note_format_issues(body["children"]))
        if kind not in {"paragraph", "quote", "heading_1", "heading_2", "heading_3",
                        "bulleted_list_item", "numbered_list_item"}:
            continue
        rich = body.get("rich_text") or []
        value = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                        for part in rich)
        if kind == "paragraph" and any(
            line.lstrip().startswith(("#### ", "|")) for line in value.splitlines()
        ):
            issues.append("raw heading/table")
        if any(part.get("type", "text") == "text" and _INLINE_RE.search(
            part.get("plain_text") or part.get("text", {}).get("content", "")
        ) for part in rich):
            issues.append("raw inline Markdown")
    return issues


def _plain_note_block(text: str, *, kind: str = "paragraph") -> dict:
    """Preserve campus wording as plain Notion text, including math syntax."""
    rich = [{"type": "text", "text": {"content": text[i:i + 1800]}}
            for i in range(0, len(text), 1800)] or [{"type": "text", "text": {"content": ""}}]
    return {"object": "block", "type": kind, kind: {"rich_text": rich}}


def formal_assignment_blocks(job: RecordingJob) -> list[dict]:
    """Official nearby course assignments, clearly separate from lecture notes."""
    from .campus_catalog import campus_course
    from .notion_home import _linked_paragraph, _source_key, _source_state

    blocks = [_plain_note_block("本课程近期正式作业", kind="heading_2")]
    blocks.append(_plain_note_block(
        "以下来自教学网课程作业目录：有截止时间的条目限本节录像当天至其后 30 天；"
        "未公布截止时间的正式作业单列，需核对是否仍有效。"
        "不代表老师在这节课布置。题目若含图片、矩阵或表格，必须打开原件核对完整题目。"
    ))
    try:
        course = campus_course(job.course_dir.parent, job.recording.course_id)
    except ValueError:
        blocks.append(_plain_note_block("课程作业目录尚未同步，请登录教学网核对正式要求。"))
        return blocks
    try:
        recorded = datetime.fromisoformat(job.recording.date).date()
    except ValueError:
        blocks.append(_plain_note_block("录像日期无法核对，请登录教学网查看本课程作业。"))
        return blocks
    source_state = _source_state(job.course_dir.parent)

    def append_card_link(item: dict) -> None:
        entry = source_state.get(_source_key(job.recording.course_id, "作业", item))
        page_id = str(entry.get("id") or "") if isinstance(entry, dict) else ""
        if re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", page_id):
            blocks.append(_linked_paragraph(
                "在 Notion 查看正式作业卡片",
                "https://www.notion.so/" + page_id.replace("-", "")))
    attendance_pattern = r"随堂考勤|到课考勤|现场签到|课堂展示|课堂汇报|课堂点评"

    def append_formal_assignment(item: dict, due_label: str) -> None:
        blocks.append(_plain_note_block(str(item.get("title") or "未命名作业"), kind="heading_3"))
        append_card_link(item)
        blocks.append(_plain_note_block("截止：" + due_label))
        blocks.append(_plain_note_block("教学网原要求：" + str(item.get("instructions") or "原要求未能读取，请打开教学网核对。")))
        if re.search(attendance_pattern,
                     str(item.get("title") or "") + str(item.get("instructions") or "")):
            blocks.append(_plain_note_block("涉及必须到场或现场参与的要求；笔记不能代替本人参加。"))
        linked = 0
        for asset in item.get("attachments") or []:
            if not isinstance(asset, dict):
                continue
            path = _safe_attachment_path(asset.get("path"))
            if not path:
                continue
            from urllib.parse import quote
            blocks.append(_linked_paragraph(
                "登录教学网打开原题附件：" + str(asset.get("filename") or "附件"),
                "https://course.pku.edu.cn" + quote(path, safe="/%")))
            linked += 1
        if item.get("files") and not linked:
            blocks.append(_plain_note_block("原题附件暂无安全直达链接，请从教学网提交入口下载并核对原件。"))
        content_id = str(item.get("content_id") or "")
        if item.get("source") != "calendar" and re.fullmatch(r"_\d+_\d+", content_id):
            from urllib.parse import quote
            target = ("https://course.pku.edu.cn/webapps/assignment/uploadAssignment"
                      "?course_id=" + quote(job.recording.course_id, safe="")
                      + "&content_id=" + quote(content_id, safe=""))
            blocks.append(_linked_paragraph("打开教学网提交入口", target))
        else:
            blocks.append(_linked_paragraph("登录教学网查找正式提交入口", "https://course.pku.edu.cn/"))

    nearby = []
    undated_attendance = []
    undated_other = []
    for item in course.get("assignments", []):
        if not isinstance(item, dict):
            continue
        raw_due = str(item.get("due_at") or "")
        try:
            due = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
            if due.tzinfo is not None:
                due = due.astimezone(timezone(timedelta(hours=8)))
            due_date = due.date()
        except ValueError:
            if re.search(attendance_pattern,
                         str(item.get("title") or "") + str(item.get("instructions") or "")):
                undated_attendance.append(item)
            else:
                due_label = ("教学网未公布；请核对老师最新通知和作业原页。" if not raw_due else
                             "教学网截止时间无法解析；请打开作业原页核对。")
                undated_other.append((item, due_label))
            continue
        if recorded <= due_date <= recorded + timedelta(days=30):
            due_label = (due.strftime("%Y-%m-%d %H:%M 北京时间")
                         if "T" in raw_due or " " in raw_due else
                         due.strftime("%Y-%m-%d") + "（教学网未提供具体时间）")
            nearby.append((due, due_label, item))
    nearby.sort(key=lambda pair: (pair[0], str(pair[2].get("title") or "")))
    if not nearby:
        blocks.append(_plain_note_block("该时间范围内暂未检出有明确截止日期的正式作业；请以教学网实时目录为准。"))
    for _, due_label, item in nearby:
        append_formal_assignment(item, due_label)
    if undated_other:
        blocks.append(_plain_note_block("正式作业（截止时间待核对）", kind="heading_3"))
        for item, due_label in undated_other:
            append_formal_assignment(item, due_label)
    if undated_attendance:
        blocks.append(_plain_note_block("课程目录中的到场事项（时间待核对）", kind="heading_3"))
        for item in undated_attendance:
            blocks.append(_plain_note_block(str(item.get("title") or "未命名到场事项"), kind="heading_3"))
            append_card_link(item)
            blocks.append(_plain_note_block(
                "教学网原要求：" + str(item.get("instructions") or "原要求未能读取，请打开教学网核对。")))
            blocks.append(_plain_note_block(
                "教学网目录未提供具体举行时间；涉及必须到场或现场参与，笔记不能代替本人参加。"
                "请查看课程最新通知确认安排。"))
            content_id = str(item.get("content_id") or "")
            if re.fullmatch(r"_\d+_\d+", content_id):
                from urllib.parse import quote
                target = ("https://course.pku.edu.cn/webapps/assignment/uploadAssignment"
                          "?course_id=" + quote(job.recording.course_id, safe="")
                          + "&content_id=" + quote(content_id, safe=""))
                blocks.append(_linked_paragraph(
                    "在教学网查看原事项（现场安排以课程通知为准）", target))
    if course.get("assignments_status") in {"partial", "error", "not_synced"}:
        blocks.append(_plain_note_block("作业目录尚未完整同步，以上列表可能不全。"))
    blocks.append(_plain_note_block("课程目录读取：" + str(course.get("synced_at_label") or "未记录") + "。"))
    return blocks


def assignment_snapshot_issue(course: dict, *, now: datetime | None = None) -> str | None:
    """Require a recently refreshed official assignment index at publication."""
    raw = str(course.get("synced_at") or "")
    try:
        synced = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "教学网课程目录缺少可核对的读取时间；请刷新课程目录后再发布。"
    if synced.tzinfo is None:
        return "教学网课程目录读取时间缺少时区；请刷新课程目录后再发布。"
    age = (now or datetime.now(timezone.utc)) - synced
    if age < -timedelta(minutes=5):
        return "教学网课程目录读取时间晚于本机时间；请检查系统时钟并刷新课程目录。"
    if age > timedelta(hours=24):
        return "教学网正式作业目录已超过 24 小时未刷新；请到「我的课程」点击「更新课程目录」后再发布。"
    return None


def assignment_catalog_publish_issues(job: RecordingJob) -> list[str]:
    from .campus_catalog import campus_course

    try:
        course = campus_course(job.course_dir.parent, job.recording.course_id)
    except ValueError:
        return ["教学网课程及正式作业目录尚未同步；请到「我的课程」更新课程目录后再整理。"]
    if course.get("assignments_status") != "available":
        return ["教学网正式作业目录尚未完整同步；请到「我的课程」更新课程目录后再整理。"]
    issue = assignment_snapshot_issue(course)
    return [issue] if issue else []


def note_publish_issues(job: RecordingJob, note_path: Path) -> list[str]:
    """Check evidence available on disk before presenting a note as complete.

    A missing courseware match is not itself a failure: some classes have no
    slides. Only a known contradiction is blocked. The formal homework index,
    however, must have completed a course refresh before a new note can claim
    to show the course's current assignments.
    """
    from types import SimpleNamespace

    from ..lecture_source_download import prepare_lesson_sources
    from ..media import _stamp, note_coverage_gap_seconds, note_source_conflicts
    from ..note_quality import ambiguous_public_key_segment, audit_note, note_factual_conflicts
    from ..note_sources import collect_note_sources
    from .transcript_review import review_pending

    issues: list[str] = []
    if note_path == job.directory / "notes.md" and review_pending(job):
        issues.append("转写术语已听音校正，原笔记尚未重新整理；请重新整理后再发布。")
    transcript_path = job.directory / "transcript.json"
    try:
        transcript = json.loads(transcript_path.read_text("utf-8"))
        if not isinstance(transcript, dict) or not transcript.get("segments"):
            raise ValueError("empty transcript")
    except (OSError, ValueError):
        return ["本机缺少完整转写，无法核对整节覆盖；请重新整理这节录像。"]
    gap = note_coverage_gap_seconds(note_path, transcript)
    if gap > 90:
        issues.append(f"笔记比录像转写提前约 {round(gap / 60, 1)} 分钟结束；请重新整理这节录像。")
    ambiguous_match = ambiguous_public_key_segment(transcript["segments"])
    if ambiguous_match:
        ambiguous = ambiguous_match[1]
        issues.append("录像 " + _stamp(float(ambiguous.get("start") or 0))
                      + " 附近的 RAC／RSA 术语尚未听音核实；请核对课堂原音后再发布。")
    issues.extend(assignment_catalog_publish_issues(job))

    # Reuse verified local lecture attachments without opening a campus
    # session. Missing attachments are disclosed in the note, not guessed.
    paths, _ = prepare_lesson_sources(
        job, recording_id(job), SimpleNamespace(pku_username="", pku_password="")
    )
    sources = collect_note_sources(job.course_dir, job.recording.date,
                                   job.recording.title, allowed_paths=paths)
    readable = [row for row in sources if row.get("status") == "readable" and row.get("text")]
    conflicts = note_source_conflicts(note_path.read_text("utf-8"), readable)
    if conflicts:
        issues.append("笔记与已关联课件矛盾：" + "；".join(conflicts[:2]) + "。请核对原课件并重新整理。")
    factual = note_factual_conflicts(note_path.read_text("utf-8"), readable)
    if factual:
        issues.append("笔记有已知事实或引用错误：" + "；".join(factual[:3]) + "。请核对并重新整理。")
    # These two deterministic findings from the broader offline audit are
    # actionable before publication. Other audit checks require evidence and
    # human signoff that the automatic student workflow does not yet collect.
    report = audit_note(note_path.read_text("utf-8"), transcript, sources=sources)
    for check in report["checks"]:
        if check["name"] == "课业否定断言" and check["status"] == "fail":
            issues.append("笔记把未查到的课业写成不存在：" + check["detail"])
        elif check["name"] == "自然排版" and check["status"] == "review":
            issues.append("笔记仍像时间段提纲或原始转写，请整理成自然段落后再发布。")
    return issues


def _note_source_signature(block: dict) -> tuple:
    """Compare app-owned blocks by what readers see and where links lead."""
    kind = block.get("type") or ""
    rich = (block.get(kind) or {}).get("rich_text") or []
    spans: list[tuple[str, str]] = []
    for part in rich:
        value = part.get("plain_text")
        if value is None:
            value = (part.get("text") or {}).get("content") or ""
        link = ((part.get("text") or {}).get("link") or {}).get("url") or part.get("href") or ""
        if spans and spans[-1][1] == link:
            spans[-1] = (spans[-1][0] + value, link)
        else:
            spans.append((value, link))
    return kind, tuple(spans)


def _recording_note_stamp(title: str, original_title: str, revision_base: str) -> str | None:
    """Return a sortable revision stamp for a note owned by this recording."""
    if title == original_title:
        return ""
    match = re.search(r" · (\d{4}-\d{2}-\d{2} \d{2}:\d{2} 北京时间) · [0-9a-f]{4}$", title)
    if match and title[:match.start()] == revision_base[:100 - len(match.group(0))]:
        return match.group(1)
    return None


def publish_notes(job: RecordingJob, lecture_id: str, notion_token: str, *,
                  note_path: Path | None = None, revision: bool = False) -> str:
    from ..notion import NotionClient, markdown_to_blocks
    from .note_images import publish_note_images
    from .note_videos import publish_note_videos

    title = f"转写笔记 · {job.recording.date} {job.recording.title}"[:100]
    revision_base = f"更新笔记 · {job.recording.date} {job.recording.title}"
    body = (note_path or job.directory / "notes.md").read_text("utf-8")
    if not body.strip():
        raise ValueError("笔记文件为空，尚不能发布到 Notion。")
    # Legacy callers may publish a hand-written note with no saved transcript.
    # The student recording flow always saves one and must pass this gate.
    if (job.directory / "transcript.json").is_file():
        issues = note_publish_issues(job, note_path or job.directory / "notes.md")
        if issues:
            raise ValueError("笔记暂不能作为完整课程笔记发布：" + "；".join(issues))
    rendered = markdown_to_blocks(body)
    if _note_format_issues(rendered):
        raise ValueError("笔记包含未能转换的 Markdown，请先修复排版再发布。")
    source_blocks = formal_assignment_blocks(job)
    if "与本讲明确关联的课件文件：" not in body:
        source_blocks.append(_plain_note_block(
            "核对范围：本页未记录可核对课件的完整引用；专业术语、数值及题目请对照老师原件。"
        ))
    from .notion_home import LECTURE_PLACEHOLDER

    with NotionClient(notion_token) as notion:
        candidates = []
        for index, page in enumerate(notion.list_child_pages(lecture_id)):
            stamp = _recording_note_stamp(page.get("title") or "", title, revision_base)
            if stamp is not None:
                candidates.append((stamp, index, page))
        existing = max(candidates)[2] if candidates else None
        created_id = ""
        if existing and not revision:
            old_blocks = notion.list_children(existing["id"])
            expected_blocks = source_blocks + rendered
            revision = (len(old_blocks) < len(expected_blocks) or any(
                _note_source_signature(old) != _note_source_signature(current)
                for old, current in zip(old_blocks, expected_blocks)
            ))
        if existing and not revision:
            note_id = existing["id"]
            url = f"https://www.notion.so/{note_id.replace('-', '')}"
        else:
            if revision:
                # Keep student annotations on the earlier page intact.
                stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M 北京时间")
                suffix = f" · {stamp} · {uuid.uuid4().hex[:4]}"
                title = revision_base[:100 - len(suffix)] + suffix
            page = notion.create_page(lecture_id, title, children=source_blocks + rendered, retry=False)
            note_id = page["id"]
            created_id = note_id
            url = page.get("url") or f"https://www.notion.so/{note_id.replace('-', '')}"
        try:
            publish_note_images(notion, note_id, job.directory)
            publish_note_videos(notion, note_id, job.directory)
            published_blocks = notion.list_children(note_id)
            if _note_format_issues(published_blocks[len(source_blocks):] if created_id else published_blocks):
                raise ValueError("Notion 页面仍包含原样 Markdown，发布未通过排版验收。")
            for block in notion.list_children(lecture_id):
                if block.get("type") != "paragraph":
                    continue
                rich = (block.get("paragraph") or {}).get("rich_text") or []
                text = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                               for part in rich)
                if text == LECTURE_PLACEHOLDER:
                    notion.archive_block(block["id"], retry=False)
        except Exception:
            # Only a page created by this invocation may be rolled back.
            if created_id:
                try:
                    notion.archive_page(created_id, retry=False)
                except Exception:
                    logger.exception("could not archive failed new note page")
            raise
        return url


class RecordingWorkManager:
    def __init__(self, settings, directory_service):
        self.settings = settings
        self.directory_service = directory_service
        self._lock = threading.Lock()
        self._cancel_sync = threading.Event()
        self._work: Work | None = None

    def state(self) -> dict:
        with self._lock:
            return self._work.as_dict() if self._work else {"state": "idle"}

    def _stage(self, value: str) -> None:
        with self._lock:
            if self._work:
                self._work.stage = value
                self._work.progress_done = 0
                self._work.progress_total = 0
                self._work.progress_remaining = 0
                self._work.progress_percent = 0
                self._work.progress_unit = ""
                self._work.bytes_done = 0
                self._work.speed_bps = 0
                self._progress_started = time.monotonic()

    def _advance(self, value: str) -> None:
        """Move to the next declared stage of a task that has a fixed plan."""
        with self._lock:
            if self._work and self._work.stage_total:
                self._work.stage_index = min(self._work.stage_index + 1,
                                             self._work.stage_total)
        self._stage(value)

    def _plan(self, stage_total: int, first_stage: str = "") -> None:
        """Declare how many coarse stages this task will pass through."""
        with self._lock:
            if self._work:
                self._work.stage_total = max(0, stage_total)
                self._work.stage_index = 1 if stage_total else 0
                if first_stage:
                    self._work.stage = first_stage

    def _progress(self, done: int, total: int, unit: str, bytes_done: int,
                  label: str = "") -> None:
        with self._lock:
            if self._work and self._work.state == "running":
                self._work.progress_done = max(0, done)
                self._work.progress_total = max(0, total)
                self._work.progress_remaining = max(0, self._work.progress_total - self._work.progress_done)
                self._work.progress_unit = unit
                self._work.progress_percent = (min(100, int(self._work.progress_done / self._work.progress_total * 100))
                                               if self._work.progress_total else 0)
                if label:
                    self._work.current_item = label
                self._work.bytes_done = max(0, bytes_done)
                elapsed = max(time.monotonic() - getattr(self, "_progress_started", time.monotonic()), 0.1)
                self._work.speed_bps = int(bytes_done / elapsed) if bytes_done else 0

    def _start(self, kind: str, recording_key: str, action: Callable[[], str]) -> dict:
        with self._lock:
            if self._work and self._work.state == "running":
                raise ValueError("已有录音任务正在进行，请等待完成。")
            self._cancel_sync.clear()
            self._work = Work(kind=kind, recording_id=recording_key)
            self._progress_started = time.monotonic()
        threading.Thread(target=self._run, args=(action,), daemon=True).start()
        return self.state()

    def _run(self, action: Callable[[], str]) -> None:
        try:
            result_url = action()
        except Exception as exc:
            if self._cancel_sync.is_set() and self._work and self._work.kind == "sync":
                with self._lock:
                    self._work.state = "cancelled"
                    self._work.stage = "目录更新已取消"
                    self._work.error = ""
                return
            failure_id = uuid.uuid4().hex[:8].upper()
            failed_stage = self._work.stage if self._work else "准备任务"
            failed_kind = self._work.kind if self._work else "unknown"
            logger.exception(
                "on-demand recording job failed failure_id=%s kind=%s stage=%s",
                failure_id,
                failed_kind,
                failed_stage,
            )
            if isinstance(exc, ValueError):
                message = str(exc)
            elif self._work and self._work.kind == "sync":
                message = ("教学网目录已读取，但同步到 Notion 失败；已建立的页面会保留，请稍后重试。"
                           if "Notion" in self._work.stage else
                           "教学网连接中断，课程目录未完整更新；已有记录会保留，请检查网络后重试。")
            elif self._work and self._work.kind == "catalog":
                message = "Notion 课程目录暂时无法更新；已建立的页面会保留，请稍后重试。"
            elif self._work and self._work.kind == "summary":
                message = ("课程总结没有生成；讲次页不受影响，请检查 Notion 连接后重试。")
            else:
                message = _unexpected_process_failure(failed_stage)
            with self._lock:
                self._work.state = "failed"
                self._work.error = message
                self._work.failure_id = failure_id
        else:
            with self._lock:
                self._work.state = "done"
                if self._work.kind == "sync":
                    self._work.stage = ("课程与 Notion 页面已更新" if self._work.notion_synced
                                        else "课程目录已更新")
                    self._work.stage = self._with_summary_note(self._work.stage)
                elif self._work.kind == "catalog":
                    self._work.stage = self._with_summary_note("Notion 课程与讲次页已建立")
                elif self._work.kind == "download":
                    self._work.stage = "录像已保存到本机，可下载到电脑"
                elif self._work.kind == "summary":
                    self._work.stage = self._summary_done_text()
                else:
                    self._work.stage = "已发布到 Notion"
                self._work.result_url = result_url
                if not self._work.result_url and self._work.result_urls:
                    self._work.result_url = self._work.result_urls[0]
                if self._work.progress_total:
                    self._work.progress_done = self._work.progress_total
                    self._work.progress_remaining = 0
                    self._work.progress_percent = 100

    def _with_summary_note(self, stage: str) -> str:
        if self._work.summary_created or self._work.summary_existing:
            stage += "，并已生成课程总结"
        if self._work.warnings:
            stage += f"；{len(self._work.warnings)} 门课程总结未生成"
        return stage

    def _summary_done_text(self) -> str:
        text = (f"课程总结已生成：新建 {self._work.summary_created} 页，"
                f"当日已有 {self._work.summary_existing} 页未重复写入")
        if self._work.warnings:
            text += f"；{len(self._work.warnings)} 门未生成"
        return text

    def _course_ids(self) -> list[str]:
        from .campus_catalog import campus_courses
        return [row["id"] for row in campus_courses(Path(self.settings.data_dir))["courses"]]

    def _summarize_courses(self, course_ids: list[str], *, required: bool,
                           plan: bool = True) -> dict:
        from .course_summary import publish_course_summaries
        if not course_ids:
            if required:
                raise ValueError("还没有同步任何课程；请先在「我的课程」更新课程目录。")
            return {"requested": 0, "created": 0, "existing": 0, "urls": [], "failures": []}
        if plan:
            self._plan(len(course_ids) + 1, f"生成课程总结：共 {len(course_ids)} 门课程")
        else:
            self._advance("生成课程总结")
        self._progress(0, len(course_ids), "courses", 0)
        result = publish_course_summaries(
            Path(self.settings.data_dir), self.settings.notion_token, course_ids,
            on_course=(lambda title: self._advance(f"生成课程总结：{title}")) if plan else None,
            on_progress=lambda done, total, title: self._progress(done, total, "courses", 0,
                                                                  label=title),
        )
        with self._lock:
            if self._work:
                self._work.summary_created += result["created"]
                self._work.summary_existing += result["existing"]
                self._work.result_urls.extend(result["urls"])
                self._work.warnings.extend(f"《{title}》：{message}"
                                           for title, message in result["failures"])
        if required and result["failures"] and not result["urls"]:
            raise ValueError("课程总结没有生成：" + result["failures"][0][1])
        return result

    def _catalog_item_progress(self, done: int, total: int, label: str) -> None:
        self._progress(done, total, "items", 0, label=label)

    def _sync_notion_catalog(self) -> None:
        from .notion_home import sync_catalog
        self._plan(2, "建立 Notion 课程与讲次页")
        self._stage("建立 Notion 课程与讲次页")
        sync_catalog(Path(self.settings.data_dir), self.settings.notion_token,
                     self._stage, self._progress, self._catalog_item_progress)
        with self._lock:
            if self._work:
                self._work.notion_synced = True
        # The student's next step after the pages exist is the exam snapshot,
        # so it starts in the same task instead of waiting for another click.
        self._summarize_courses(self._course_ids(), required=False, plan=False)

    def start_course_summaries(self, course_id: str = "") -> dict:
        from .campus_catalog import campus_course
        from .notion_home import saved_home
        if not getattr(self.settings, "notion_token", ""):
            raise ValueError("请先连接 Notion。")
        root = Path(self.settings.data_dir)
        if not saved_home(root):
            raise ValueError("请先选择 Notion 父页面并创建学习主页。")
        if course_id:
            campus_course(root, course_id)
            targets = [course_id]
        else:
            targets = self._course_ids()
            if not targets:
                raise ValueError("还没有同步任何课程；请先在「我的课程」更新课程目录。")
        def run() -> str:
            result = self._summarize_courses(targets, required=True)
            return result["urls"][0] if result["urls"] else ""
        return self._start("summary", course_id, run)

    def cancel_sync(self) -> dict:
        with self._lock:
            if not self._work or self._work.state != "running" or self._work.kind != "sync":
                raise ValueError("当前没有可取消的课程目录更新。")
            self._cancel_sync.set()
            self._work.stage = "正在取消，完成当前课程读取后停止"
            return self._work.as_dict()

    def start_sync(self) -> dict:
        def cancellable_stage(value: str) -> None:
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
            self._stage(value)
        def run() -> str:
            sync_recording_index(self.settings, cancellable_stage, self._progress)
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
            from .notion_home import saved_home
            if getattr(self.settings, "notion_token", "") and saved_home(Path(self.settings.data_dir)):
                self._sync_notion_catalog()
            return ""
        return self._start("sync", "", run)

    def start_catalog_sync(self) -> dict:
        from .notion_home import saved_home
        if not getattr(self.settings, "notion_token", ""):
            raise ValueError("请先连接 Notion。")
        if not saved_home(Path(self.settings.data_dir)):
            raise ValueError("请先选择 Notion 父页面并创建学习主页。")
        return self._start("catalog", "", lambda: (self._sync_notion_catalog() or ""))

    def start_download(self, key: str) -> dict:
        job = find_job(Path(self.settings.data_dir), key)
        if job is None:
            raise ValueError("找不到这条录像，请重新同步课程目录。")
        if not self.settings.pku_username or not self.settings.pku_password:
            raise ValueError("请先填写教学网账号和密码。")
        return self._start("download", key, lambda: self._download(job, key))

    def _download(self, job: RecordingJob, key: str) -> str:
        from ..auth import get_session
        if not job.video.exists():
            self._stage("正在连接教学网")
            client = get_session(username=self.settings.pku_username,
                                 password=self.settings.pku_password)
            try:
                self._stage("下载录像到本机")
                result = download_job(client, job, progress=self._progress)
            finally:
                client.close()
            if result.path is None:
                raise ValueError("录像下载没有完成，请检查教学网连接后重试。")
        marker = job.directory / ".keep-video"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return f"/api/recordings/{key}/video"

    def start_process(self, key: str, course_id: str, lecture_id: str, direct_oss: bool = False,
                      regenerate: bool = False) -> dict:
        job = find_job(Path(self.settings.data_dir), key)
        if job is None:
            raise ValueError("找不到这条录像，请重新同步录音索引。")
        if not getattr(self.settings, "platform_token", ""):
            raise ValueError("请先登录并完成邮箱验证。")
        if not getattr(self.settings, "notion_token", ""):
            raise ValueError("请先连接 Notion。")
        from .course_recordings import source_course

        _, selected_source, _ = source_course(Path(self.settings.data_dir), self.directory_service, course_id)
        if not selected_source or job.recording.course_id != selected_source:
            raise ValueError("这条录像不属于当前课程，请先确认教学网课程来源。")
        target = _target_lecture(self.directory_service, course_id, lecture_id)
        return self._start("process", key, lambda: self._process_after_refresh(
            job, target["id"], direct_oss=direct_oss, regenerate=regenerate))

    def _refresh_stale_catalog(self, job: RecordingJob) -> None:
        """Renew an outdated campus snapshot instead of refusing to publish.

        A note states the course's current formal assignments, so the snapshot
        it reads must be fresh. Re-reading one course takes seconds; only a
        failed campus login stops the run.
        """
        issues = assignment_catalog_publish_issues(job)
        if not issues:
            return
        if (not getattr(self.settings, "pku_username", "")
                or not getattr(self.settings, "pku_password", "")):
            raise ValueError("；".join(issues)
                             + " 请先在「我的课程」保存教学网账号，应用会自动重新读取目录。")
        self._stage("教学网目录已过期，正在重新读取")
        try:
            sync_recording_index(self.settings, self._stage, self._progress,
                                 course_id=job.recording.course_id)
        except ValueError as exc:
            raise ValueError(f"教学网目录更新失败：{exc} 目录过期时不能安全生成笔记。") from exc
        except Exception as exc:
            logger.exception("campus catalog refresh failed")
            raise ValueError("教学网目录更新失败（可能是登录超时），无法核对最新作业；"
                             "请检查网络后重试。") from exc
        remaining = assignment_catalog_publish_issues(job)
        if remaining:
            raise ValueError("；".join(remaining) + " 已自动重新读取教学网目录，但仍未通过核对。")

    def _process_after_refresh(self, job: RecordingJob, lecture_id: str,
                               direct_oss: bool = False, regenerate: bool = False) -> str:
        self._refresh_stale_catalog(job)
        return self._process(job, lecture_id, direct_oss=direct_oss, regenerate=regenerate)

    def start_campus_process(self, key: str, course_id: str, direct_oss: bool = False,
                             regenerate: bool = False, reuse_existing: bool = False) -> dict:
        job = find_job(Path(self.settings.data_dir), key)
        if job is None or job.recording.course_id != course_id:
            raise ValueError("这条录像不属于当前教学网课程。")
        if not getattr(self.settings, "platform_token", ""):
            raise ValueError("请先登录并完成邮箱验证。")
        if not getattr(self.settings, "notion_token", ""):
            raise ValueError("请先连接 Notion。")
        from .notion_home import saved_home, ensure_recording_target
        root = Path(self.settings.data_dir)
        if not saved_home(root):
            raise ValueError("请先选择 Notion 父页面并创建学习主页。")
        if reuse_existing:
            required = (
                job.directory / "transcript.json",
                job.directory / "notes.md",
                job.directory / "keyframes" / "index.json",
            )
            if not all(path.is_file() for path in required):
                raise ValueError("本机已有结果不完整；请使用「整理这节录像」补齐转写、笔记和课堂画面。")
        # Publishing complete local artifacts is intentionally independent of
        # campus availability. The immutable recording metadata already on
        # disk is sufficient to create the Notion target and publish the note.
        if not reuse_existing and not (
                getattr(self.settings, "pku_username", "")
                and getattr(self.settings, "pku_password", "")):
            issues = assignment_catalog_publish_issues(job)
            if issues:
                raise ValueError("；".join(issues)
                                 + " 请先在「我的课程」保存教学网账号，应用会自动重新读取目录。")
        def run() -> str:
            if not reuse_existing:
                self._refresh_stale_catalog(job)
            self._stage("准备 Notion 课程与讲次页")
            lecture_id = ensure_recording_target(root, self.settings.notion_token, job)
            return self._process(
                job,
                lecture_id,
                direct_oss=direct_oss,
                regenerate=regenerate,
                reuse_existing=reuse_existing,
            )
        return self._start("process", key, run)

    def _process(self, job: RecordingJob, lecture_id: str, direct_oss: bool = False,
                 regenerate: bool = False, reuse_existing: bool = False) -> str:
        from ..auth import get_session
        from ..lecture_source_download import prepare_lesson_sources
        from ..media import note_coverage_gap_seconds, write_notes
        from ..note_sources import collect_note_sources
        from .transcript_review import clear_regeneration_pending, review_pending

        notes_path = job.directory / "notes.md"
        transcript_path = job.directory / "transcript.json"
        regenerate = regenerate or review_pending(job)
        if regenerate and not notes_path.is_file():
            raise ValueError("这节课还没有旧笔记，请使用「整理这节录像」。")
        if regenerate and not transcript_path.is_file():
            raise ValueError("本机没有这节课的转写，无法安全重整旧笔记。")
        if not regenerate and notes_path.exists() and not transcript_path.is_file():
            raise ValueError("本机缺少这节课的转写，无法核对笔记是否覆盖整节录像；请重新整理后再发布。")
        if not regenerate and notes_path.exists() and transcript_path.exists():
            transcript = json.loads(transcript_path.read_text("utf-8"))
            gap = note_coverage_gap_seconds(notes_path, transcript)
            if gap > 90:
                raise ValueError(
                    f"已有笔记比转写提前约 {round(gap / 60, 1)} 分钟结束；"
                    "本机原文件已保留，暂不发布到 Notion。请检查笔记后重新整理。"
                )
        catalog_issues = assignment_catalog_publish_issues(job)
        if catalog_issues:
            raise ValueError("；".join(catalog_issues))
        staged_note: Path | None = None
        if regenerate:
            source_paths, source_failures = prepare_lesson_sources(
                job, recording_id(job), self.settings, stage=self._stage
            )
            transcript = json.loads(transcript_path.read_text("utf-8"))
            source_context = collect_note_sources(
                job.course_dir, job.recording.date, job.recording.title,
                allowed_paths=source_paths,
            )
            source_context.extend(source_failures)
            staged_note = job.directory / f"notes.rebuild-{uuid.uuid4().hex}.md"
            self._stage("重新生成课堂笔记")
            try:
                written = write_notes(transcript, _saved_keyframes(job.directory),
                                      staged_note, self.settings, job.label,
                                      source_context=source_context, progress=self._progress,
                                      course_directory=job.course_dir)
                if written is None:
                    raise ValueError("笔记生成没有完成，请检查 AI 额度后重试。")
                gap = note_coverage_gap_seconds(staged_note, transcript)
                if gap > 90:
                    raise ValueError(f"新版笔记比转写提前约 {round(gap / 60, 1)} 分钟结束。")
            except Exception as exc:
                staged_note.unlink(missing_ok=True)
                if isinstance(exc, (RuntimeError, ValueError)):
                    raise ValueError(f"新版笔记没有完成：{exc}；原笔记保留。") from exc
                raise ValueError("新版笔记没有完成；原笔记保留，请稍后重试。") from exc
        elif not notes_path.exists():
            if not job.video.exists() and not transcript_path.exists():
                if not self.settings.pku_username or not self.settings.pku_password:
                    raise ValueError("请先填写教学网账号和密码，以下载这条录像。")
                self._stage("正在连接教学网")
                client = get_session(username=self.settings.pku_username,
                                     password=self.settings.pku_password)
                try:
                    self._stage("下载录像")
                    downloaded = download_job(client, job, progress=self._progress)
                finally:
                    client.close()
                if downloaded.path is None:
                    raise ValueError("录像下载没有完成，请稍后重试。")
            if job.video.exists():
                source_paths, source_failures = prepare_lesson_sources(
                    job, recording_id(job), self.settings, stage=self._stage
                )
                self._stage("转写并生成笔记")
                result = process_job(
                    job, self.settings, include_keyframes=True,
                    progress=self._progress, direct_oss=direct_oss,
                    source_paths=source_paths, source_failures=source_failures,
                    preserve_video=True,
                )
                if not result.notes:
                    logger.warning("recording processing incomplete: %s", [error.split(":", 1)[0] for error in result.errors])
                    if not result.transcribed:
                        failure = next((error.removeprefix("transcribe: ") for error in result.errors
                                        if error.startswith("transcribe: ")), "请检查录像文件或网络")
                        raise ValueError(f"转写没有完成：{failure}。已下载的录像保留，可重试。")
                    failure = next((error.removeprefix("notes: ") for error in result.errors
                                    if error.startswith("notes: ")), "请检查 AI 额度")
                    raise ValueError(f"转写已完成，但笔记未完成：{failure}")
            else:
                source_paths, source_failures = prepare_lesson_sources(
                    job, recording_id(job), self.settings, stage=self._stage
                )
                self._stage("从已有转写生成笔记")
                transcript = json.loads(transcript_path.read_text("utf-8"))
                source_context = collect_note_sources(
                    job.course_dir, job.recording.date, job.recording.title,
                    allowed_paths=source_paths,
                )
                source_context.extend(source_failures)
                written = write_notes(
                    transcript, _saved_keyframes(job.directory), notes_path,
                    self.settings, job.label, source_context=source_context,
                    progress=self._progress, course_directory=job.course_dir,
                )
                if written is None:
                    raise ValueError("笔记生成没有完成，请检查 AI 额度后重试。")
        from ..media import extract_keyframes
        frame_index = job.directory / "keyframes" / "index.json"
        frame_rows = _saved_keyframes(job.directory)
        if not frame_rows and not regenerate:
            if not job.video.exists():
                if not self.settings.pku_username or not self.settings.pku_password:
                    raise ValueError("本机没有课堂画面；请连接教学网后重试补图。")
                self._stage("重新获取录像以补充课堂画面")
                client = get_session(username=self.settings.pku_username,
                                     password=self.settings.pku_password)
                try:
                    downloaded = download_job(client, job, progress=self._progress)
                finally:
                    client.close()
                if downloaded.path is None:
                    raise ValueError("无法获取录像画面；文字笔记已保留，请稍后重试。")
            frame_index.unlink(missing_ok=True)
            self._stage("提取课堂画面")
            try:
                frame_rows = extract_keyframes(job.video, job.directory / "keyframes")
            except Exception as exc:
                logger.exception("lecture frame extraction failed")
                raise ValueError("课堂画面提取失败；文字笔记已保留，请稍后重试。") from exc
            if not frame_rows:
                raise ValueError("录像未能提取课堂画面；文字笔记已保留，请稍后重试。")
        from .note_videos import needs_video_clips
        if not reuse_existing and not job.video.exists() and needs_video_clips(notes_path):
            if self.settings.pku_username and self.settings.pku_password:
                self._stage("获取录像以生成回看片段")
                client = get_session(username=self.settings.pku_username,
                                     password=self.settings.pku_password)
                try:
                    downloaded = download_job(client, job, progress=self._progress)
                finally:
                    client.close()
                if downloaded.path is None:
                    logger.warning("video clip source download failed: %s", downloaded.error)
        self._stage("将课堂画面和笔记发布到 Notion")
        try:
            if staged_note is not None:
                note_url = publish_notes(job, lecture_id, self.settings.notion_token,
                                         note_path=staged_note, revision=True)
            else:
                note_url = publish_notes(job, lecture_id, self.settings.notion_token)
        except Exception as exc:
            logger.exception("Notion note publication failed")
            quality_message = (str(exc) if isinstance(exc, ValueError)
                               and str(exc).startswith("笔记暂不能作为完整课程笔记发布") else "")
            if staged_note is not None:
                staged_note.unlink(missing_ok=True)
                if quality_message:
                    raise ValueError(f"{quality_message}；原笔记及旧页面均已保留。") from exc
                raise ValueError("新版笔记未能写入 Notion；原笔记及旧页面均已保留，可稍后重试。") from exc
            if quality_message:
                raise ValueError(f"{quality_message}；本机笔记已保留。") from exc
            raise ValueError("笔记已保存在本机，但写入 Notion 失败。请检查连接后重试。") from exc
        from .note_videos import video_publication_complete
        if (getattr(self.settings, "delete_video_after_processing", False)
                and not (job.directory / ".keep-video").exists()
                and video_publication_complete(job.directory)):
            job.video.unlink(missing_ok=True)
        if staged_note is not None:
            backup = job.directory / f"notes.previous-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.md"
            notes_path.replace(backup)
            try:
                staged_note.replace(notes_path)
            except Exception:
                backup.replace(notes_path)
                raise
        from .notion_home import saved_home
        publication = {
            "recording_id": recording_id(job),
            "home_id": saved_home(Path(self.settings.data_dir)).get("id", ""),
            "lecture_id": lecture_id,
            "lecture_url": f"https://www.notion.so/{lecture_id.replace('-', '')}",
            "note_url": note_url,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        marker = job.directory / "notion-publication.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(marker.name + ".tmp")
        temporary.write_text(json.dumps(publication, ensure_ascii=False, indent=1), "utf-8")
        temporary.replace(marker)
        if regenerate:
            clear_regeneration_pending(job)
        with self._lock:
            if self._work:
                self._work.lecture_url = publication["lecture_url"]
        return note_url


def add_recording_routes(app: FastAPI, manager: RecordingWorkManager, *, demo_connection: bool = False) -> None:
    @app.get("/api/campus/courses")
    def campus_courses_endpoint() -> dict:
        from .campus_catalog import campus_courses
        payload = campus_courses(Path(manager.settings.data_dir))
        payload["campus_configured"] = bool(getattr(manager.settings, "pku_username", "") and getattr(manager.settings, "pku_password", ""))
        payload["demo_connection"] = demo_connection
        return payload

    @app.get("/api/campus/courses/{course_id}")
    def campus_course_endpoint(course_id: str) -> dict:
        from .campus_catalog import campus_course
        try:
            return campus_course(Path(manager.settings.data_dir), course_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/campus/courses/{course_id}/recordings/{key}/term-review")
    def recording_term_review(course_id: str, key: str) -> dict:
        from .transcript_review import review_status
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or job.recording.course_id != course_id:
            raise HTTPException(status_code=404, detail="这节录像不属于该课程。")
        return review_status(job, key)

    @app.post("/api/campus/courses/{course_id}/recordings/{key}/term-review")
    def confirm_recording_term(course_id: str, key: str, body: TermReviewConfirmation) -> dict:
        from .transcript_review import confirm_rsa
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or job.recording.course_id != course_id:
            raise HTTPException(status_code=404, detail="这节录像不属于该课程。")
        with manager._lock:
            if manager._work and manager._work.state == "running":
                raise HTTPException(status_code=409, detail="当前有任务正在运行，请完成后再校对转写。")
            try:
                return confirm_rsa(job, segment_index=body.segment_index,
                                   transcript_sha256=body.transcript_sha256,
                                   confirmed_spoken_term=body.confirmed_spoken_term,
                                   confirmed=body.confirmed)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/campus/courses/{course_id}/existing-notion")
    def save_existing_notion_link(course_id: str, body: ExistingNotionLink) -> dict:
        from urllib.parse import urlsplit
        from .campus_catalog import campus_course

        value = body.url.strip()
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not
            (parsed.hostname in {"notion.so", "www.notion.so", "app.notion.com"}
             or (parsed.hostname or "").endswith(".notion.site"))):
            raise HTTPException(status_code=400, detail="请输入已有 Notion 课程页的 HTTPS 链接。")
        root = Path(manager.settings.data_dir)
        try:
            campus_course(root, course_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        folder = next((path.parent for path in root.glob("*/course.json")
                       if json.loads(path.read_text("utf-8")).get("course_id") == course_id), None)
        if folder is None:
            raise HTTPException(status_code=404, detail="课程尚未同步。")
        path = folder / "course.json"
        metadata = json.loads(path.read_text("utf-8"))
        metadata["existing_notion_url"] = value
        temporary = path.with_name("course.json.tmp")
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=1), "utf-8")
        temporary.replace(path)
        return campus_course(root, course_id)

    @app.post("/api/campus/courses/{course_id}/materials/{material_index}/match")
    def campus_material_match(course_id: str, material_index: int, body: MaterialMatchRequest) -> dict:
        from .campus_catalog import campus_course
        root = Path(manager.settings.data_dir)
        try:
            course = campus_course(root, course_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        materials = course["all_materials"]
        if material_index < 0 or material_index >= len(materials):
            raise HTTPException(status_code=404, detail="找不到这项资料。")
        if not body.ignore and body.lecture_id and body.lecture_id not in {row["id"] for row in course["lessons"]}:
            raise HTTPException(status_code=422, detail="请选择本课程的录像讲次。")
        folder = next((path.parent for path in root.glob("*/course.json")
                       if json.loads(path.read_text("utf-8")).get("course_id") == course_id), None)
        if folder is None:
            raise HTTPException(status_code=404, detail="课程尚未同步。")
        marker = folder / "materials" / "matches.json"
        try:
            overrides = json.loads(marker.read_text("utf-8"))
            if not isinstance(overrides, dict):
                overrides = {}
        except (OSError, ValueError):
            overrides = {}
        key = materials[material_index]["path"]
        if body.ignore:
            overrides[key] = "ignore"
        elif body.lecture_id:
            overrides[key] = body.lecture_id
        else:
            overrides.pop(key, None)
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(overrides, ensure_ascii=False), "utf-8")
        temporary.replace(marker)
        return campus_course(root, course_id)

    @app.get("/api/campus/courses/{course_id}/materials/{material_index}/files/{file_index}")
    def campus_material_file(course_id: str, material_index: int, file_index: int):
        """Resolve a fresh Blackboard attachment URL with the user's local session."""
        from urllib.parse import quote
        from ..auth import get_session
        from ..lecture_source_download import _safe_webdav_url
        from ..materials import walk_materials
        from .campus_catalog import _courses, campus_course
        from .handbook_requirements import cache_handbook_pdf

        if not manager.settings.pku_username or not manager.settings.pku_password:
            raise HTTPException(status_code=409, detail="请先保存教学网账号。")
        root = Path(manager.settings.data_dir)
        try:
            course = campus_course(root, course_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        indexed = course.get("all_materials", [])
        if material_index < 0 or material_index >= len(indexed):
            raise HTTPException(status_code=404, detail="课件不存在，请更新目录。")
        row = indexed[material_index]
        if file_index < 0 or file_index >= len(row["files"]):
            raise HTTPException(status_code=404, detail="附件不存在，请更新目录。")
        client = get_session(username=manager.settings.pku_username,
                             password=manager.settings.pku_password)
        try:
            live, _ = walk_materials(client, course_id)
            matching = next((item for item in live
                             if item.path == row["path"] and item.title == row["title"]), None)
            if matching is None:
                raise HTTPException(status_code=404, detail="课件已变动，请更新课程目录。")
            filename = row["files"][file_index]
            asset = next((item for item in matching.attachments
                          if item.filename == filename), None)
            if asset is None:
                raise HTTPException(status_code=404, detail="附件已变动，请更新课程目录。")
            if not _safe_webdav_url(asset.url):
                raise HTTPException(status_code=502, detail="教学网返回了无效的附件地址。")
            response = client.get(asset.url)
            response.raise_for_status()
            if not _safe_webdav_url(str(getattr(response, "url", asset.url))):
                raise HTTPException(status_code=502, detail="教学网附件跳转到了无效地址。")
            cached = False
            if any(card["material_index"] == material_index and card["file_index"] == file_index
                   and card["material_path"] == row["path"] and card["filename"] == filename
                   for card in course.get("handbook_reviews", [])):
                folder = next((path for path, key, _, _ in _courses(root)
                               if key == course_id), None)
                if folder is not None:
                    try:
                        cached = cache_handbook_pdf(folder / "materials", row, filename,
                                                    response.content)
                    except OSError:
                        logger.warning("course handbook cache failed")
            return StreamingResponse(iter((response.content,)),
                media_type=response.headers.get("content-type", "application/octet-stream"),
                headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename),
                         "X-Handbook-Cached": "true" if cached else "false"})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("campus material download failed")
            raise HTTPException(status_code=502, detail="暂时无法读取教学网附件，请稍后再试。") from exc
        finally:
            client.close()

    @app.get("/api/campus/notion/parents")
    def notion_parents_endpoint() -> dict:
        from .notion_home import available_parents, saved_home
        token = getattr(manager.settings, "notion_token", "")
        if not token:
            detail = ("演示模式只模拟 Notion 连接，不会读取真实页面；请退出演示模式后授权 Notion。"
                      if demo_connection else "请先连接 Notion。")
            raise HTTPException(status_code=409, detail=detail)
        try:
            home = saved_home(Path(manager.settings.data_dir))
            return {"pages": available_parents(token), "home": home}
        except Exception as exc:
            logger.exception("Notion parent listing failed")
            raise HTTPException(status_code=502, detail="Notion 页面暂时无法读取，请稍后重试。") from exc

    @app.post("/api/campus/notion/home")
    def notion_home_endpoint(body: HomeRequest) -> dict:
        from .notion_home import create_home
        token = getattr(manager.settings, "notion_token", "")
        if not token:
            raise HTTPException(status_code=409, detail="请先连接 Notion。")
        try:
            home = create_home(Path(manager.settings.data_dir), token, body.parent_page_id)
            from ..platform import publish_account_profile
            home["profile_sync"] = publish_account_profile(manager.settings)
            from .campus_catalog import campus_courses
            if campus_courses(Path(manager.settings.data_dir))["courses"]:
                if manager.state().get("state") == "running":
                    home["task"] = manager.state()
                else:
                    home["task"] = manager.start_catalog_sync()
            return home
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Notion learning home creation failed")
            raise HTTPException(status_code=502, detail="学习主页没有创建成功，请稍后重试。") from exc

    @app.post("/api/campus/notion/home/move")
    def notion_home_move_endpoint(body: HomeRequest) -> dict:
        from .notion_home import move_home
        token = getattr(manager.settings, "notion_token", "")
        if not token:
            raise HTTPException(status_code=409, detail="请先连接 Notion。")
        try:
            home = move_home(Path(manager.settings.data_dir), token, body.parent_page_id)
            from ..platform import publish_account_profile
            home["profile_sync"] = publish_account_profile(manager.settings)
            return home
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Notion learning home move failed")
            raise HTTPException(status_code=502, detail="学习主页没有移动成功，请检查 Notion 页面权限后重试。") from exc

    @app.get("/api/recordings")
    def recordings() -> dict:
        return {"items": list_recordings(Path(manager.settings.data_dir)),
                "campus_configured": bool(getattr(manager.settings, "pku_username", "") and getattr(manager.settings, "pku_password", ""))}

    @app.get("/api/courses/{course_id}/recordings")
    def course_recordings(course_id: str) -> dict:
        from .course_recordings import rows_for_course

        try:
            payload = rows_for_course(Path(manager.settings.data_dir), manager.directory_service, course_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload["campus_configured"] = bool(getattr(manager.settings, "pku_username", "") and getattr(manager.settings, "pku_password", ""))
        payload["demo_connection"] = demo_connection
        return payload

    @app.post("/api/courses/{course_id}/recording-source")
    def recording_source(course_id: str, body: CourseSourceRequest) -> dict:
        from .course_recordings import choose_source, rows_for_course

        try:
            root = Path(manager.settings.data_dir)
            choose_source(root, manager.directory_service, course_id, body.campus_course_id)
            return rows_for_course(root, manager.directory_service, course_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/recordings/credentials")
    def credentials(body: CampusCredentials) -> dict:
        username = body.username.strip()
        if not username:
            raise HTTPException(status_code=400, detail="请输入教学网账号。")
        write_env_values(env_file_path(manager.settings),
                         {"PKU_USERNAME": username, "PKU_PASSWORD": body.password})
        manager.settings.pku_username = username
        manager.settings.pku_password = body.password
        return {"configured": True}

    @app.post("/api/recordings/sync")
    def sync() -> dict:
        try:
            return manager.start_sync()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/recordings/cancel-sync")
    def cancel_sync() -> dict:
        try:
            return manager.cancel_sync()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/recordings/task")
    def task() -> dict:
        return manager.state()

    @app.post("/api/campus/notion/sync")
    def notion_catalog_sync() -> dict:
        try:
            return manager.start_catalog_sync()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/campus/notion/summaries")
    def notion_summaries() -> dict:
        try:
            return manager.start_course_summaries()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/campus/courses/{course_id}/summary")
    def campus_course_summary(course_id: str) -> dict:
        try:
            return manager.start_course_summaries(course_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/campus/courses/{course_id}/recordings/{key}/duration")
    def campus_recording_duration(course_id: str, key: str) -> dict:
        """Read player metadata on demand without fetching video or charging quota."""
        from ..auth import get_session
        from ..recordings import resolve_media

        if not manager.settings.pku_username or not manager.settings.pku_password:
            raise HTTPException(status_code=409, detail="请先保存教学网账号。")
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or job.recording.course_id != course_id:
            raise HTTPException(status_code=404, detail="这节录像不属于该课程。")
        client = None
        try:
            client = get_session(username=manager.settings.pku_username,
                                 password=manager.settings.pku_password)
            resolve_media(client, job.recording)
            duration = max(0, int(job.recording.duration_seconds or 0))
            if duration:
                job.directory.mkdir(parents=True, exist_ok=True)
                path = job.directory / "duration.json"
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps({"duration_seconds": duration}), "utf-8")
                temporary.replace(path)
            return {"duration_seconds": duration}
        except Exception as exc:
            logger.exception("recording duration lookup failed")
            raise HTTPException(status_code=502, detail="暂时无法读取教学网录像时长，请稍后重试。") from exc
        finally:
            if client is not None:
                client.close()

    @app.post("/api/campus/courses/{course_id}/recordings/{key}/process")
    def campus_process(course_id: str, key: str, body: CampusProcessRequest | None = None) -> dict:
        try:
            return manager.start_campus_process(
                key, course_id, direct_oss=body.direct_oss if body else False,
                regenerate=body.regenerate if body else False,
                reuse_existing=body.reuse_existing if body else False)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/recordings/{key}/download")
    def download(key: str) -> dict:
        try:
            return manager.start_download(key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/recordings/{key}/video")
    def video_file(key: str):
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or not job.video.is_file():
            raise HTTPException(status_code=404, detail="这条录像尚未下载到本机。")
        filename = safe_name(f"{job.recording.date}_{job.recording.title}", "recording") + ".mp4"
        return FileResponse(job.video, media_type="video/mp4", filename=filename)

    @app.get("/api/recordings/{key}/preview")
    def preview_video(key: str):
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or not job.video.is_file():
            raise HTTPException(status_code=404, detail="这条录像尚未下载到本机。")
        filename = safe_name(f"{job.recording.date}_{job.recording.title}", "recording") + ".mp4"
        return FileResponse(job.video, media_type="video/mp4", filename=filename,
                            content_disposition_type="inline")

    @app.post("/api/recordings/{key}/process")
    def process(key: str, body: OrganizeRequest) -> dict:
        try:
            return manager.start_process(key, body.course_id, body.lecture_id,
                                         direct_oss=body.direct_oss, regenerate=body.regenerate)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
