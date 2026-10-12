"""Explicit, one-recording-at-a-time desktop workflow for lecture notes."""
from __future__ import annotations

import hashlib
import errno
import json
import logging
import re
import shutil
import sqlite3
import threading
import time
import uuid
from copy import copy
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..envfile import write_env_values
from ..notion_login import env_file_path
from ..notion import NotionError
from ..pipeline import RecordingJob, collect_jobs, download_job, process_job, recording_stage
from ..store import safe_name

logger = logging.getLogger(__name__)
_PORTAL_STATUS_LOCK = threading.RLock()
_ACTIVE_COURSE_DIRECTORIES = {}


def _portal_status_write(write, *args, **kwargs):
    # Only the local ledger read/replace is atomic; no remote request is locked.
    with _PORTAL_STATUS_LOCK:
        return write(*args, **kwargs)


@contextmanager
def _reserve_course_directories(root, courses):
    from ..course_storage import plan_course_directories
    owner = object()
    with _PORTAL_STATUS_LOCK:
        result = plan_course_directories(root, courses)
        keys = [str(path.resolve()).casefold() for path in result.values()]
        if any(key in _ACTIVE_COURSE_DIRECTORIES for key in keys):
            raise ValueError("课程目录正在更新；已有文件保留，请等待当前更新结束后重试。")
        for key in keys:
            _ACTIVE_COURSE_DIRECTORIES[key] = owner
    try:
        yield result
    finally:
        with _PORTAL_STATUS_LOCK:
            for key in keys:
                if _ACTIVE_COURSE_DIRECTORIES.get(key) is owner:
                    _ACTIVE_COURSE_DIRECTORIES.pop(key)


class _CourseWrites:
    def __init__(self):
        self.lock = threading.RLock()
        self.owners = {}
        self.projection = threading.Lock()

    def reserve(self, account, course_id, owner):
        with self.lock:
            for (scope, course), current in self.owners.items():
                if scope == account and (course == course_id or '*' in (course, course_id)):
                    raise ValueError("这门课已有更新或整理任务，请等当前任务完成后重试。")
            self.owners[(account, course_id)] = owner

    def release(self, account, course_id, owner):
        with self.lock:
            if self.owners.get((account, course_id)) is owner:
                self.owners.pop((account, course_id))



def _disk_space_failure(exc: BaseException) -> bool:
    """Classify typed local storage failures without exposing private messages."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError) and (
            exc.errno == errno.ENOSPC or getattr(exc, 'winerror', None) in (39, 112)
        ):
            return True
        if isinstance(exc, sqlite3.Error) and getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class CampusCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=300)


class CourseRefreshRequest(BaseModel):
    course_id: str = Field(default="", max_length=120)
    scope: str = ""
    resume: bool = False


class OrganizeRequest(BaseModel):
    course_id: str
    lecture_id: str
    direct_oss: bool = False
    regenerate: bool = False
    notes_strategy: Literal["transcript", "topics"] | None = None


class CampusProcessRequest(BaseModel):
    direct_oss: bool = False
    regenerate: bool = False
    reuse_existing: bool = False
    notes_strategy: Literal["transcript", "topics"] | None = None


class CourseSourceRequest(BaseModel):
    campus_course_id: str

class HomeRequest(BaseModel):
    parent_page_id: str
    replace_existing: bool = False


class SummaryAcceptanceRequest(BaseModel):
    notes_sha256: str = Field(min_length=64, max_length=64)
    confirmed: bool = False


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
    course_id: str = ""
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
    catalog_updated: int = 0
    catalog_incomplete: int = 0
    summary_created: int = 0
    summary_existing: int = 0
    result_urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failure_id: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    scope: str = "foreground"

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
    from ..catalog_snapshots import recording_source_id
    try:
        source = recording_source_id(job.recording)
    except ValueError:
        return legacy_recording_id(job)
    identity = "\n".join((job.recording.course_id, source))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def legacy_recording_id(job: RecordingJob) -> str:
    identity = "\n".join((job.recording.course_id, job.recording.recorded_at,
                          job.recording.title, job.course_dir.name))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def publication_matches_job(job: RecordingJob, publication: dict) -> bool:
    from ..catalog_snapshots import recording_source_id
    try:
        source = recording_source_id(job.recording)
    except ValueError:
        source = ""
    if publication.get("course_id") and publication["course_id"] != job.recording.course_id:
        return False
    if publication.get("source_id"):
        return bool(source and publication["source_id"] == source)
    if publication.get("recording_id") in {recording_id(job), legacy_recording_id(job)}:
        return True
    # Older markers can survive a title change when their published-version
    # evidence binds this exact native session to the same note and lecture.
    try:
        version = json.loads((job.directory / "published-note-version.json").read_text("utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(version, dict):
        return False
    note_id = str(version.get("note_id") or "").replace("-", "")
    try:
        note_path = urlsplit(str(publication.get("note_url") or "")).path.replace("-", "")
    except ValueError:
        return False
    return bool(source and version.get("source_id") == source and note_id
                and note_path.endswith(note_id) and version.get("lecture_id")
                and version["lecture_id"] == publication.get("lecture_id"))


def find_job(data_dir: Path, key: str) -> RecordingJob | None:
    matches = [job for job in collect_jobs(data_dir)
               if key in {recording_id(job), legacy_recording_id(job)}]
    return matches[0] if len(matches) == 1 else None


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


def _confirmation_snapshot(course, read, deadlines):
    from .. import catalog_snapshots as catalog
    from ..materials import merge_deadlines
    if (read.recordings_error is not None or read.materials_error is not None
            or (read.notices_error is not None and read.notice_status != 'unavailable')
            or any(not isinstance(rows, list) for rows in
                   (read.recordings, read.material_items, read.tree_assignments, read.notices))):
        raise catalog.CatalogUnavailable("再次读取教学网目录不完整，未删除云端条目，请稍后重新更新。")
    material_rows = [{"title": item.title, "kind": item.kind, "path": item.path,
        "content_id": item.content_id, "body_text": item.body_text, "parent_content_id": item.parent_content_id,
        "files": [asset.filename for asset in item.attachments],
        "source_links": [link.model_dump() for link in item.source_links],
        "attachments": [{"filename": asset.filename, "path": path} for asset in item.attachments
                        if (path := _safe_attachment_path(asset.url))]} for item in read.material_items]
    assignment_rows = [{"title": item.title, "content_id": item.content_id, "source_id": item.source_id,
        "due_at": item.due_at, "source": item.source, "instructions": item.instructions,
        "files": [asset.filename for asset in item.attachments],
        "source_links": [link.model_dump() for link in item.source_links]}
        for item in merge_deadlines(read.tree_assignments, deadlines.get(course.course_id, []))]
    notice_rows = [{"id": item.announcement_id, "title": item.title, "posted_at": item.posted_at,
        "author": item.author, "body_text": item.body_text,
        "files": [asset.filename for asset in item.attachments]} for item in read.notices]
    return catalog.build_snapshot(course, read.recordings, material_rows, assignment_rows, notice_rows)


def _confirm_source_replacement(settings, client, course, observation, snapshot, stage):
    from .. import catalog_snapshots as catalog
    from ..discover import discover_courses, select_courses
    from ..materials import fetch_deadlines, fetch_announcements, walk_materials
    from ..recordings import list_recordings as fetch_recordings
    from ..tui_data import read_course_metadata
    stage(f"再次完整核对教学网目录，清理云端过时条目：{course.name}")
    base = catalog.current(settings, course.course_id)
    confirmation = catalog.begin_observation(settings, course.course_id)
    if base['revision'] != observation['revision'] or confirmation['revision'] != observation['revision']:
        raise catalog.CatalogUnavailable("核对期间云端目录已变化，本次没有删除条目，请重新读取课程目录。")
    courses = select_courses(discover_courses(client), settings)
    selected = [row for row in courses if row.course_id == course.course_id]
    if len(selected) != 1:
        raise catalog.CatalogUnavailable("再次读取未能确认这门教学网课程，未删除云端条目。")
    try:
        deadlines = fetch_deadlines(client)
        reads = read_course_metadata(client, [course.course_id], recordings_fn=fetch_recordings,
                                    materials_fn=walk_materials, announcements_fn=fetch_announcements,
                                    check_cancel=lambda: stage(f"再次核对课程内容：{course.name}"))
    except Exception as exc:
        raise catalog.CatalogUnavailable("再次读取教学网目录失败，未删除云端条目，请检查连接后重新更新。") from exc
    confirmed = _confirmation_snapshot(selected[0], reads[course.course_id], deadlines)
    if confirmed != snapshot:
        raise catalog.CatalogUnavailable("两次教学网读取结果不一致，本次没有删除云端条目，请稍后重新更新。")
    stage(f"教学网目录已再次确认，更新云端目录：{course.name}")
    return {'observation_id': confirmation['observation_id'],
            'base_snapshot_sha256': catalog.fingerprint(base['snapshot']),
            'snapshot_sha256': catalog.fingerprint(confirmed)}


def sync_recording_index(settings, stage: Callable[[str], None], progress=None,
                         course_id: str = "", on_course_confirmed=None) -> None:
    """Read the campus index into the local data directory.

    ``course_id`` limits the refresh to one course, so a stale snapshot can be
    renewed in seconds before a note is published instead of re-reading every
    course the student has.
    """
    from ..auth import get_session
    from ..discover import discover_courses, select_courses
    from ..recordings import list_recordings as fetch_recordings
    from ..materials import (fetch_announcements, fetch_deadlines,
                             merge_deadlines, walk_materials)
    from ..store import safe_name
    from ..models import Assignment
    from .. import catalog_snapshots as catalog

    root = Path(settings.data_dir)
    # Reserve modest metadata headroom before changing confirmation state or
    # requesting new revisions. Resident NTFS files can still be rewritten
    # on a full volume, which would otherwise invalidate usable old records.
    existing = next(path for path in (root, *root.parents) if path.exists())
    if shutil.disk_usage(existing).free < 65536:
        raise OSError(errno.ENOSPC, 'Insufficient space for course metadata')
    catalog.activate_account(settings)

    if not settings.pku_username or not settings.pku_password:
        raise ValueError("请先填写教学网账号和密码。")
    stage("登录教学网")
    client = get_session(username=settings.pku_username, password=settings.pku_password)
    directory_reservation = None
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
        # Fence *all* official facts, including calendar deadlines and course
        # titles, before reading them. Getting a ticket only after the calendar
        # read would rebase an older deadline onto another device's new revision.
        observations = {}
        observation_issues = {}
        for course in courses:
            # The task's stage callback also checks explicit cancellation.
            # Check between external reads, before invalidating usable records.
            stage(f"核对课程目录：{course.name}")
            try:
                observations[course.course_id] = catalog.begin_observation(settings, course.course_id)
            except catalog.CatalogTransportUnavailable:
                # A disconnected account endpoint affects the whole attempt.
                # Stop before mutating confirmed state or reading each course;
                # per-course revision/protocol refusals still remain partial.
                raise
            except catalog.CatalogUnavailable as exc:
                observations[course.course_id] = None
                if isinstance(exc, catalog.CatalogProjectionBusy):
                    observation_issues[course.course_id] = "projection_busy"
        if any(observations.values()):
            stage("核对教学网课程列表")
            fresh = {course.course_id: course for course in
                     select_courses(discover_courses(client), settings)}
            if any(course.course_id not in fresh for course in courses):
                raise ValueError("教学网课程列表读取不一致，保留只读旧目录。")
            courses = [fresh[course.course_id].model_copy(update={"dir_name": course.directory})
                       for course in courses]
        # Only replace confirmation state after login, discovery and account
        # observations completed without a global connection failure.
        stage("准备更新课程记录")
        if course_id:
            _portal_status_write(catalog.read_status, root, False, "reading", course_id=course_id)
        else:
            _portal_status_write(catalog.read_status, root, False, "reading")
        directory_reservation = _reserve_course_directories(root, courses)
        course_directories = directory_reservation.__enter__()
        try:
            deadlines_by_course = fetch_deadlines(client)
            calendar_available = True
        except Exception:
            logger.exception("campus calendar deadlines unavailable")
            deadlines_by_course = {}
            calendar_available = False
        if progress:
            progress(0, len(courses), "courses", 0)
        from ..tui_data import read_course_metadata
        metadata = read_course_metadata(
            client, [course.course_id for course in courses],
            recordings_fn=fetch_recordings, materials_fn=walk_materials,
            announcements_fn=fetch_announcements,
            check_cancel=lambda: stage("读取教学网课程元数据"),
        )
        complete_attempt = True
        shared_confirmed = True
        for course_number, course in enumerate(courses, 1):
            observation = observations[course.course_id]
            account_issue = observation_issues.get(course.course_id, "")
            if observation is None:
                shared_confirmed = False
            stage(f"同步录音索引：{course.name}")
            if progress:
                progress(course_number - 1, len(courses), "courses", 0)
            course_dir = course_directories[course.course_id]
            _portal_status_write(catalog.course_read_status, root, course.course_id, False, "reading")
            read = metadata[course.course_id]
            if read.recordings_error is not None:
                complete_attempt = False
                exc = read.recordings_error
                logger.error("campus recording index unavailable for %s", course.course_id,
                             exc_info=(type(exc), exc, exc.__traceback__))
                _portal_status_write(catalog.course_read_status, root, course.course_id, False, "recordings_unavailable")
                stage(f"教学网录像不可用，保留只读旧目录：{course.name}")
                continue
            recordings = read.recordings
            stage(f"读取课件目录：{course.name}")
            if progress:
                progress(course_number - 1, len(courses), "courses", 0)
            if read.materials_error is not None:
                exc = read.materials_error
                logger.error("campus material index unavailable for %s", course.course_id,
                             exc_info=(type(exc), exc, exc.__traceback__))
                material_items, tree_assignments = None, []
            else:
                material_items, tree_assignments = read.material_items, read.tree_assignments
            notices, notice_status = read.notices, read.notice_status
            if read.notices_error is not None and notice_status != "unavailable":
                exc = read.notices_error
                logger.error("campus announcements unavailable for %s", course.course_id,
                             exc_info=(type(exc), exc, exc.__traceback__))
            course_dir = course_directories[course.course_id]
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
            if material_items is None or not calendar_available or notices is None:
                complete_attempt = False
                _portal_status_write(catalog.course_read_status, root, course.course_id, False, "partial")
                # Preserve the last full data and its original fetched timestamp.
                # Status is diagnostic, never evidence of a successful new read.
                if previous_meta:
                    stale = {**previous_meta,
                             "announcements_status": notice_status,
                             "assignments_status": meta["assignments_status"],
                             "snapshot_complete": False}
                    temporary = previous_meta_path.with_suffix(".json.tmp")
                    temporary.write_text(json.dumps(stale, ensure_ascii=False, indent=1), "utf-8")
                    temporary.replace(previous_meta_path)
                stage(f"教学网读取不完整，保留只读旧目录：{course.name}")
                continue
            meta["snapshot_complete"] = True
            index_dir.mkdir(parents=True, exist_ok=True)
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
                    "source_id": item.source_id,
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
            course_confirmed = False
            if observation is not None:
                try:
                    snapshot = catalog.build_snapshot(course, recordings, material_rows,
                                                      assignment_rows, notice_rows)
                    try:
                        envelope = catalog.publish(settings, course.course_id, observation, snapshot)
                    except catalog.CatalogSourceRemovalRequired:
                        confirmation = _confirm_source_replacement(settings, client, course, observation, snapshot, stage)
                        envelope = catalog.publish(settings, course.course_id, observation, snapshot,
                                                   removal_confirmation=confirmation)
                    catalog.verify_current(settings, envelope)
                    catalog.save_cache(root, envelope)
                    course_confirmed = True
                except (ValueError, catalog.CatalogUnavailable) as exc:
                    shared_confirmed = False
                    if isinstance(exc, catalog.CatalogProjectionBusy):
                        account_issue = "projection_busy"
                    # No rebase: keep a newer account snapshot if one is readable.
                    stage(f"账号课程快照未确认；共享目录保持只读：{course.name}")
                    try:
                        catalog.save_cache(root, catalog.current(settings, course.course_id))
                    except catalog.CatalogUnavailable:
                        pass
            for destination, payload in (
                (course_dir / "course.json", meta),
                (index_dir / "index.json", [item.model_dump() for item in recordings]),
            ):
                temporary = destination.with_name(destination.name + ".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
                temporary.replace(destination)
            _portal_status_write(catalog.course_read_status, root, course.course_id, course_confirmed,
                                       "complete" if course_confirmed else "account_unconfirmed",
                                       issue=account_issue)
            if course_confirmed and on_course_confirmed is not None:
                on_course_confirmed(course.course_id, envelope)
            if progress:
                progress(course_number, len(courses), "courses", 0)
        # Each course must have been a complete observation in this attempt.
        reason = ("complete" if complete_attempt and shared_confirmed
                  else "partial" if not complete_attempt else "account_unconfirmed")
        if course_id:
            _portal_status_write(catalog.read_status, root, complete_attempt and shared_confirmed, reason,
                                course_id=course_id)
        else:
            _portal_status_write(catalog.read_status, root, complete_attempt and shared_confirmed, reason)
    finally:
        if directory_reservation is not None:
            directory_reservation.__exit__(None, None, None)
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
    """Check publication state and bound review records, never prose patterns."""
    from ..note_editor import review_record_issues
    from .transcript_review import review_pending

    issues = []
    if note_path == job.directory / "notes.md" and review_pending(job):
        issues.append("转写已校正，原笔记尚未重新整理；请重新整理后再发布。")
    try:
        transcript = json.loads((job.directory / "transcript.json").read_text("utf-8"))
        if not isinstance(transcript, dict) or not transcript.get("segments"):
            raise ValueError("empty transcript")
    except (OSError, ValueError):
        return ["本机缺少完整转写，请重新整理这节录像。"]
    issues.extend(review_record_issues(note_path, transcript))
    issues.extend(assignment_catalog_publish_issues(job))
    return issues


def _note_rich_signature(rich: list[dict]) -> tuple:
    """Normalize transport convenience fields, retaining text, links and styles."""
    spans = []
    for part in rich:
        kind = part.get("type", "text")
        if kind == "equation":
            value = (part.get("equation") or {}).get("expression") or ""
        elif kind == "mention":
            value = json.dumps(part.get("mention") or {}, ensure_ascii=False, sort_keys=True)
        else:
            value = part.get("plain_text")
            if value is None:
                value = (part.get("text") or {}).get("content") or ""
        link = ((part.get("text") or {}).get("link") or {}).get("url") or part.get("href") or ""
        annotations = {key: val for key, val in (part.get("annotations") or {}).items()
                       if val is not False and val != "default"}
        style = json.dumps(annotations, sort_keys=True)
        if kind == "text" and spans and spans[-1][0] == kind and spans[-1][2:] == (link, style):
            spans[-1] = (kind, spans[-1][1] + value, link, style)
        else:
            spans.append((kind, value, link, style))
    return tuple(spans)


def _note_source_signature(block: dict) -> tuple:
    """Compare submitted content and formatting without judging its meaning."""
    kind = block.get("type") or ""
    body = block.get(kind) or {}
    attributes = {key: body[key] for key in (
        "expression", "language", "table_width", "has_column_header", "has_row_header"
    ) if key in body}
    if "cells" in body:
        attributes["cells"] = [_note_rich_signature(cell) for cell in body["cells"]]
    return kind, _note_rich_signature(body.get("rich_text") or []), json.dumps(attributes, ensure_ascii=False, sort_keys=True)


def _note_blocks_match(saved: list[dict], submitted: list[dict], client=None, *, exact=False) -> bool:
    """Verify text and nested cells, permitting the app's top-level media inserts."""
    index = 0
    for block in saved:
        if index == len(submitted):
            return not exact  # Later student annotations and media are retained.
        expected = submitted[index]
        if _note_source_signature(block) == _note_source_signature(expected):
            kind = expected.get("type") or ""
            expected_body = expected.get(kind) or {}
            if "children" in expected_body:
                children = (block.get(kind) or {}).get("children")
                if children is None:
                    if not block.get("has_children") or not block.get("id") or client is None:
                        return False
                    children = client.list_children(block["id"])
                if not isinstance(children, list) or not _note_blocks_match(
                        children, expected_body["children"], client, exact=True):
                    return False
            index += 1
            continue
        kind = block.get("type")
        if not exact and kind in {"image", "video"}:
            caption = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                              for part in (block.get(kind) or {}).get("caption") or [])
            if caption.startswith(("课堂画面 ", "课堂录像 ")):
                continue
        return False
    return index == len(submitted)


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
    source_blocks = list(formal_assignment_blocks(job))
    source_blocks.append(_plain_note_block(
        "回看提示：请结合本讲原录像核对待核事项；课件不能替代老师的实际讲述。"
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
            revision = not _note_blocks_match(old_blocks, expected_blocks, notion)
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
            publish_note_images(notion, note_id, job.directory,
                                notes_path=note_path or job.directory / "notes.md",
                                body_offset=len(source_blocks))
            publish_note_videos(notion, note_id, job.directory,
                                notes_path=note_path or job.directory / "notes.md")
            published_blocks = notion.list_children(note_id)
            expected_blocks = source_blocks + rendered
            if not _note_blocks_match(published_blocks, expected_blocks, notion):
                raise ValueError("Notion 回读内容与本次提交不一致，发布未完成；本机笔记保留。")
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
        from .summary_acceptance import document_fingerprint
        from ..catalog_snapshots import recording_source_id
        version = {"note_id": note_id, "lecture_id": lecture_id,
                   "source_id": recording_source_id(job.recording),
                   "notes_sha256": hashlib.sha256((note_path or job.directory / "notes.md").read_bytes()).hexdigest(),
                   "remote_sha256": document_fingerprint(notion, note_id)}
        marker = job.directory / "published-note-version.json"
        temporary = marker.with_name(marker.name + ".tmp")
        temporary.write_text(json.dumps(version, ensure_ascii=False), "utf-8")
        temporary.replace(marker)
        return url


class RecordingWorkManager:
    def __init__(self, settings, directory_service):
        self.settings = settings
        self.directory_service = directory_service
        self._lock = threading.RLock()
        self._cancel_sync = threading.Event()
        self._work: Work | None = None
        self._writes = _CourseWrites()
        self._finished = threading.Event()
        self._finished.set()
        self._background = None
        self._background_cancel = threading.Event()
        self._background_child = None
        self._owner_check = lambda: None
        self._background_recovered = False
        self._restore_background()

    def _restore_background(self):
        # Local startup read only: progress is explicitly interrupted and never authority.
        try:
            value = json.loads(self._background_path().read_text("utf-8"))
            rows = value.get("course_results")
            if (value.get("account_binding") != self._account_binding()
                    or value.get("scope") != "background" or value.get("kind") != "sync"
                    or not isinstance(rows, list) or not rows):
                return
            identifiers = set()
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("course_id"), str)
                        or not row["course_id"] or row["course_id"] in identifiers
                        or type(row.get("attempts")) is not int or not 0 <= row["attempts"] <= 3):
                    return
                identifiers.add(row["course_id"])
                row.update(state="interrupted", metadata_state="not_read", projection_state="not_read", retryable=True)
            value.update(state="interrupted", current_course_id="", stage="上次后台更新已中断；缓存可查看，请显式恢复后重新核验课程")
            self._background = value
            self._background_binding = value["account_binding"]
            self._background_recovered = True
        except (OSError, ValueError, AttributeError, TypeError):
            return

    def state(self) -> dict:
        with self._lock:
            current = self._account_binding()
            visible_work = self._work is not None and (not hasattr(self, "_reservation") or self._reservation[0] == current)
            value = self._work.as_dict() if visible_work else {"state": "idle"}
            if self._background is not None and self._background_binding == current:
                value["background_sync"] = self._background_snapshot()
            return value

    def _background_snapshot(self):
        from copy import deepcopy
        return deepcopy(self._background) if self._background else {"state": "idle", "scope": "background", "kind": "sync"}

    def background_state(self):
        with self._lock:
            if self._background is not None and self._background_binding != self._account_binding():
                return {"state": "idle", "scope": "background", "kind": "sync"}
            return self._background_snapshot()

    def _account_binding(self):
        from ..catalog_snapshots import fingerprint
        return fingerprint({"root": str(Path(self.settings.data_dir).resolve()),
                            "session": getattr(self.settings, "platform_token", ""),
                            "endpoint": getattr(self.settings, "cloud_transcribe_url", ""),
                            "notion": getattr(self.settings, "notion_token", ""),
                            "campus": getattr(self.settings, "pku_username", "")})

    def _background_path(self):
        from ..catalog_snapshots import fingerprint
        namespace = fingerprint({"session": getattr(self.settings, "platform_token", "")})
        return Path(self.settings.data_dir) / ".account-catalog" / namespace / "background-sync-state.json"

    def _save_background(self):
        with self._lock:
            value = self._background_snapshot()
            value["account_binding"] = self._background_binding
            path = self._background_state_path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
            temporary.replace(path)

    def _background_check(self):
        if self._background_cancel.is_set():
            raise ValueError("后台课程更新已取消。")
        if self._account_binding() != self._background_binding:
            raise ValueError("账号或连接已改变，后台更新已停止；请在当前账号重新开始。")

    def _background_course_ids(self, stage):
        from ..auth import get_session
        from ..discover import discover_courses, select_courses
        if not self.settings.pku_username or not self.settings.pku_password:
            raise ValueError("请先填写教学网账号和密码。")
        stage("读取后台更新课程列表")
        settings = getattr(self, "_background_settings", self.settings)
        client = get_session(username=settings.pku_username, password=settings.pku_password)
        try:
            identifiers = [row.course_id for row in select_courses(discover_courses(client), settings)]
            if not identifiers or len(set(identifiers)) != len(identifiers):
                raise ValueError("教学网课程列表为空或身份重复，请重新读取。")
            return identifiers
        finally:
            client.close()

    def _background_worker(self):
        child = RecordingWorkManager(copy(getattr(self, "_background_settings", self.settings)), self.directory_service)
        child._writes = self._writes
        child._owner_check = self._background_check if self._background else lambda: None
        return child

    def _run_background_course(self, identifier):
        child = self._background_worker()
        with self._lock:
            self._background_child = child
        try:
            waiting_since = time.monotonic()
            while True:
                self._background_check()
                if time.monotonic() - waiting_since >= 30:
                    raise ValueError("这门课的前台任务仍未完成，后台已停止等待；请稍后恢复更新。")
                try:
                    child.start_sync(identifier)
                    break
                except ValueError as exc:
                    if "这门课" not in str(exc):
                        raise
                    with self._lock:
                        self._background["stage"] = "这门课正在前台使用，后台等待当前任务完成"
                    # No permit or official read starts while this course is busy.
                    self._background_cancel.wait(.1)
            while not child._finished.wait(.05):
                if self._background_cancel.is_set() or self._account_binding() != self._background_binding:
                    child._cancel_sync.set()
                with self._lock:
                    self._background["stage"] = child.state().get("stage", "更新课程")
            return child.state()
        finally:
            with self._lock:
                self._background_child = None

    def _course_unknown_write(self, identifier):
        root = Path(self.settings.data_dir)
        for path in (root / ".notion-projection-intents").glob("*/*.json"):
            try:
                value = json.loads(path.read_text("utf-8"))
                binding = value.get("binding", {})
                if binding.get("course_id") == identifier and value.get("pending") is not None:
                    return True
            except (OSError, ValueError, AttributeError):
                return True
        from ..catalog_snapshots import fingerprint, projection_journal_path
        paths = [projection_journal_path(self.settings, identifier)]
        paths.extend((root / ".projection-receipts").glob("*/" + fingerprint({"course": identifier}) + ".json"))
        for path in paths:
            if not path.exists():
                continue
            try:
                value = json.loads(path.read_text("utf-8"))
                if value.get("phase") in {"writing", "writing-unknown"}:
                    return True
            except (OSError, ValueError, AttributeError):
                return True
        return False

    def start_background_sync(self, resume=False):
        with self._lock:
            if self._background and self._background["state"] == "running":
                raise ValueError("后台课程更新正在进行，请查看进度或取消。")
            binding = self._account_binding()
            previous = self._background
            recovered = resume and (previous is None or self._background_recovered)
            if recovered and previous is None:
                try:
                    previous = json.loads(self._background_path().read_text("utf-8"))
                except (OSError, ValueError):
                    previous = None
            if resume:
                if not previous or previous.get("account_binding", getattr(self, "_background_binding", None)) != binding:
                    raise ValueError("没有当前账号可恢复的后台更新，请重新开始。")
                if not isinstance(previous.get("course_results"), list) or not previous["course_results"]:
                    raise ValueError("后台进度记录不完整，请重新开始。")
                identifiers = set()
                for row in previous["course_results"]:
                    if (not isinstance(row, dict) or not isinstance(row.get("course_id"), str)
                            or not row["course_id"] or row["course_id"] in identifiers
                            or type(row.get("attempts")) is not int or not 0 <= row["attempts"] <= 3
                            or row.get("state") not in {"pending", "running", "done", "failed", "cancelled", "interrupted"}):
                        raise ValueError("后台进度记录无效，请重新开始。")
                    identifiers.add(row["course_id"])
                    if recovered:
                        # Cached progress never authorizes a course or trusts a done flag.
                        # Revalidate the original complete course contract on restart.
                        row.update(state="pending", metadata_state="not_read", projection_state="not_read", attempts=0, retryable=True)
                if previous.get("state") == "running":
                    # Disk status is progress, never success or permission to replay.
                    for row in previous.get("course_results", []):
                        if row["state"] == "running":
                            row.update(state="interrupted", retryable=True)
            self._background_binding = binding
            self._background_recovered = False
            self._background_settings = copy(self.settings)
            self._background_state_path = self._background_path()
            self._background_cancel.clear()
            self._background = {"id": uuid.uuid4().hex, "kind": "sync", "scope": "background",
                                "state": "running", "stage": "准备逐课后台更新", "current_course_id": "",
                                "course_results": previous["course_results"] if resume else [],
                                "progress_done": 0, "progress_total": 0, "progress_remaining": 0,
                                "progress_percent": 0, "progress_unit": "courses", "warnings": [], "error": ""}
            self._save_background()
        try:
            threading.Thread(target=self._run_background, args=(), daemon=True).start()
        except BaseException:
            with self._lock:
                self._background.update(state="failed", error="后台任务没有启动，请稍后恢复。")
            self._save_background()
            raise
        return self.background_state()

    def cancel_background_sync(self):
        with self._lock:
            if (not self._background or self._background["state"] != "running"
                    or self._background_binding != self._account_binding()):
                raise ValueError("当前没有可取消的后台更新。")
            self._background_cancel.set()
            self._background["stage"] = "正在取消，保留已完成课程并等待当前安全阶段结束"
            child = self._background_child
            if child is not None:
                child._cancel_sync.set()
            return self._background_snapshot()

    def _run_background(self):
        try:
            self._background_check()
            rows = self._background["course_results"]
            if not rows:
                identifiers = self._background_course_ids(lambda value: self._background_check())
                rows.extend({"course_id": cid, "state": "pending", "metadata_state": "not_read",
                             "projection_state": "not_read", "summary_created": 0, "summary_existing": 0,
                             "error": "", "failure_id": "", "attempts": 0, "retryable": True}
                            for cid in identifiers)
            with self._lock:
                self._background["progress_total"] = len(rows)
                self._background["progress_done"] = sum(row["state"] == "done" for row in rows)
                self._background["progress_remaining"] = len(rows) - self._background["progress_done"]
            self._save_background()
            for row in rows:
                self._background_check()
                if row["state"] == "done":
                    continue
                if self._course_unknown_write(row["course_id"]):
                    row.update(state="failed", retryable=False, error="这门课有尚未核验的写入，后台不会重放；请先核对原任务结果。")
                    continue
                if row["attempts"] >= 3:
                    row.update(state="failed", retryable=False, error="本次更新已尝试三次，请检查课程来源和连接后重新开始。")
                    continue
                with self._lock:
                    row.update(state="running", attempts=row["attempts"] + 1)
                    self._background["current_course_id"] = row["course_id"]
                self._save_background()
                try:
                    result = self._run_background_course(row["course_id"])
                except ValueError as exc:
                    if self._background_cancel.is_set() or self._account_binding() != self._background_binding:
                        row.update(state="interrupted", retryable=True)
                        raise
                    result = {"state": "failed", "error": str(exc)}
                with self._lock:
                    row.update(state=result["state"], metadata_state="confirmed" if result.get("catalog_updated") == 1 else "unconfirmed",
                               projection_state="confirmed" if result.get("notion_synced") else "failed" if result.get("catalog_updated") == 1 and result["state"] == "failed" else "not_read",
                               summary_created=result.get("summary_created", 0), summary_existing=result.get("summary_existing", 0),
                               error=result.get("error", ""), failure_id=result.get("failure_id", ""),
                               retryable=result["state"] != "done" and row["attempts"] < 3)
                    row.setdefault("attempt_history", []).append({"attempt": row["attempts"], "state": row["state"],
                                                                  "metadata_state": row["metadata_state"], "projection_state": row["projection_state"],
                                                                  "failure_id": row["failure_id"], "error": row["error"]})
                    self._background["progress_total"] = len(rows)
                    self._background["progress_done"] = sum(item["state"] == "done" for item in rows)
                    self._background["progress_remaining"] = len(rows) - self._background["progress_done"]
                    self._background["progress_percent"] = int(self._background["progress_done"] / len(rows) * 100)
                self._save_background()
            with self._lock:
                self._background["state"] = "done" if all(row["state"] == "done" for row in rows) else "partial"
                self._background["stage"] = "逐课更新已完成" if self._background["state"] == "done" else "部分课程更新失败；已完成课程可用，请检查失败结果后恢复"
        except Exception as exc:
            logger.exception("background course refresh stopped")
            with self._lock:
                self._background["state"] = "cancelled" if self._background_cancel.is_set() else "failed"
                self._background["error"] = str(exc) if isinstance(exc, ValueError) else "后台更新中断，已有课程保留，请检查连接后恢复。"
        finally:
            with self._lock:
                self._background["current_course_id"] = ""
            self._save_background()

    @contextmanager
    def _projection_stage(self):
        # Whole-manifest writes remain serial even when unrelated metadata reads overlap.
        while not self._writes.projection.acquire(timeout=.1):
            self._check_task_owner()
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
        try:
            self._check_task_owner()
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
            yield
        finally:
            self._writes.projection.release()

    def _check_task_owner(self):
        self._owner_check()
        if hasattr(self, "_reservation") and self._account_binding() != self._reservation[0]:
            raise ValueError("账号或连接已改变，当前任务已停止；请在当前账号重新开始。")

    def _stage(self, value: str) -> None:
        self._check_task_owner()
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

    def _start(self, kind: str, recording_key: str, action: Callable[[], str], *, course_id="") -> dict:
        with self._lock:
            if self._work and self._work.state == "running":
                raise ValueError("已有录音任务正在进行，请等待完成。")
            if not self._finished.is_set():
                raise ValueError("上一任务正在完成收尾，请稍后重试。")
            course_id = course_id or (recording_key if kind in {"sync", "summary"} else "*")
            account = self._account_binding()
            self._writes.reserve(account, course_id, self)
            self._reservation = (account, course_id)
            self._finished.clear()
            self._cancel_sync.clear()
            self._work = Work(kind=kind, recording_id=recording_key,
                              course_id=course_id if course_id != "*" else "")
            self._progress_started = time.monotonic()
            # The POST acknowledges this task, even if it finishes and another
            # request starts a new one before this response is returned.
            accepted = self.state()
        try:
            threading.Thread(target=self._run, args=(action,), daemon=True).start()
        except BaseException:
            with self._lock:
                self._work.state = "failed"
                self._work.error = "本机任务没有启动，请稍后重试。"
                self._writes.release(*self._reservation, self)
                self._finished.set()
            raise
        with self._lock:
            current = self.state()
            return current if current.get("id") == accepted.get("id") else accepted

    def _run(self, action: Callable[[], str]) -> None:
        try:
            self._run_action(action)
        finally:
            # Admission stays closed until the previous reservation and its
            # completion event have both been committed under the same lock.
            with self._lock:
                if hasattr(self, "_reservation"):
                    self._writes.release(*self._reservation, self)
                self._finished.set()

    def _run_action(self, action: Callable[[], str]) -> None:
        try:
            self._check_task_owner()
            if self._work and self._work.kind == 'sync':
                from ..platform import request_scope
                with request_scope():
                    result_url = action()
            else:
                result_url = action()
            self._check_task_owner()
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
            if _disk_space_failure(exc):
                message = "本机磁盘空间不足，任务未完成；已有记录会保留。请清理数据所在磁盘、释放空间后重试。"
            elif isinstance(exc, PermissionError):
                message = "本机数据文件无法写入；已有记录已保留。请检查文件是否被占用以及数据目录的写入权限后重试。"
            elif isinstance(exc, ValueError):
                message = str(exc)
            elif self._work and self._work.kind == "sync":
                message = ("教学网目录已读取，但同步到 Notion 失败；已建立的页面会保留，请稍后重试。"
                           if isinstance(exc, NotionError) or "Notion" in self._work.stage else
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
                    if self._work.catalog_incomplete:
                        self._work.state = "partial"
                        suffix = "，已验证课程已同步到 Notion" if self._work.notion_synced else ""
                        self._work.stage = (f"已更新 {self._work.catalog_updated} 门课程{suffix}；"
                                            f"{self._work.catalog_incomplete} 门读取不完整，"
                                            "旧记录保留为只读，请稍后重试")
                    else:
                        subject = "这门课" if self._work.course_id else "课程"
                        self._work.stage = (subject + "与 Notion 页面已更新" if self._work.notion_synced
                                            else subject + "目录已更新")
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

    def _sync_notion_catalog(self, course_id: str = "", *, pending_read_plan=None,
                            confirmed_course_ids=None) -> None:
        from .notion_home import sync_catalog
        def catalog_stage(value: str) -> None:
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
            self._stage(value)
        self._plan(2, "建立 Notion 课程与讲次页")
        catalog_stage("建立 Notion 课程与讲次页")
        scope = {"course_id": course_id} if course_id else {}
        if pending_read_plan is not None and pending_read_plan.enabled:
            scope.update(pending_read_plan=pending_read_plan,
                         operation_nonce=pending_read_plan.operation_nonce)
        with self._projection_stage():
            sync_catalog(Path(self.settings.data_dir), self.settings.notion_token,
                         catalog_stage, self._progress, self._catalog_item_progress,
                         settings=self.settings, **scope)
        if self._cancel_sync.is_set():
            raise ValueError("目录更新已取消。")
        with self._lock:
            if self._work:
                self._work.notion_synced = True

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
            if (not self._work or self._work.state != "running" or self._work.kind != "sync"
                    or (hasattr(self, "_reservation") and self._reservation[0] != self._account_binding())):
                raise ValueError("当前没有可取消的课程目录更新。")
            self._cancel_sync.set()
            self._work.stage = "正在取消，完成当前课程读取后停止"
            return self._work.as_dict()

    def start_sync(self, course_id: str = "") -> dict:
        course_id = course_id.strip()
        if not course_id:
            return self.start_background_sync()
        def cancellable_stage(value: str) -> None:
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
            self._stage(value)
        def refresh(pending_read_plan) -> str:
            confirmed = []
            def on_course_confirmed(identifier, envelope):
                pending_read_plan.offer(identifier, envelope)
                confirmed.append(identifier)
            if course_id:
                sync_recording_index(self.settings, cancellable_stage, self._progress,
                                     course_id=course_id, on_course_confirmed=on_course_confirmed)
            else:
                sync_recording_index(self.settings, cancellable_stage, self._progress,
                                     on_course_confirmed=on_course_confirmed)
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
            status_path = Path(self.settings.data_dir) / "portal-read-status.json"
            if course_id:
                from .. import catalog_snapshots as catalog
                # An unrelated confirmed course cannot make a failed selected
                # refresh succeed, even if the old whole-portal flag was true.
                if not status_path.exists():
                    raise ValueError("这门课的教学网目录尚未确认；已有记录保留，请重新更新。")
                selected_status = json.loads(status_path.read_text("utf-8")).get("courses", {}).get(course_id, {})
                if selected_status.get("complete") is not True:
                    if selected_status.get("issue") == "projection_busy":
                        raise catalog.CatalogProjectionBusy()
                    raise ValueError("这门课的教学网目录尚未完整确认；已有记录保留，请稍后重试。")
                catalog.assert_portal_readable(Path(self.settings.data_dir), course_id)
            if status_path.exists():
                status = json.loads(status_path.read_text("utf-8"))
                courses = status.get("courses", {})
                if course_id:
                    courses = {course_id: courses.get(course_id, {})}
                updated = sum(row.get("complete") is True for row in courses.values())
                incomplete = len(courses) - updated
                if status.get("complete") is not True and not updated:
                    if any(row.get("issue") == "projection_busy" for row in courses.values()):
                        from ..catalog_snapshots import CatalogProjectionBusy
                        raise CatalogProjectionBusy()
                    raise ValueError("教学网课程目录未能完整读取；旧记录保留为只读，请稍后重试。")
                with self._lock:
                    self._work.catalog_updated = updated
                    self._work.catalog_incomplete = incomplete
            from .notion_home import saved_home
            if getattr(self.settings, "notion_token", "") and saved_home(Path(self.settings.data_dir)):
                if course_id:
                    self._sync_notion_catalog(course_id, pending_read_plan=pending_read_plan,
                                             confirmed_course_ids=confirmed)
                else:
                    self._sync_notion_catalog(pending_read_plan=pending_read_plan,
                                             confirmed_course_ids=confirmed)
            return ""
        def run() -> str:
            from .notion_workspace import PendingWorkspaceReadPlan
            def check_prefetch_cancel():
                if self._cancel_sync.is_set():
                    raise ValueError('目录更新已取消。')
            with PendingWorkspaceReadPlan(self.settings, operation_nonce=uuid.uuid4().hex,
                                          check_cancel=check_prefetch_cancel) as plan:
                return refresh(plan)
        return self._start("sync", course_id, run)

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
        return self._start("download", key, lambda: self._download(job, key), course_id=job.recording.course_id)

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
                      regenerate: bool = False, notes_strategy: str | None = None) -> dict:
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
            job, target["id"], direct_oss=direct_oss, regenerate=regenerate,
            **({"notes_strategy":notes_strategy} if notes_strategy is not None else {})), course_id=job.recording.course_id)

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
                               direct_oss: bool = False, regenerate: bool = False,
                               notes_strategy: str | None = None) -> str:
        self._refresh_stale_catalog(job)
        return self._process(job, lecture_id, direct_oss=direct_oss, regenerate=regenerate,
            **({"notes_strategy":notes_strategy} if notes_strategy is not None else {}))

    def start_campus_process(self, key: str, course_id: str, direct_oss: bool = False,
                             regenerate: bool = False, reuse_existing: bool = False,
                             notes_strategy: str | None = None) -> dict:
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
            from .notion_workspace import saved_workspace
            with self._projection_stage():
                lecture_id = (ensure_recording_target(root, self.settings.notion_token, job, settings=self.settings)
                              if saved_workspace(root) else ensure_recording_target(root, self.settings.notion_token, job))
            return self._process(
                job,
                lecture_id,
                direct_oss=direct_oss,
                regenerate=regenerate,
                reuse_existing=reuse_existing,
                **({"notes_strategy":notes_strategy} if notes_strategy is not None else {}),
            )
        return self._start("process", key, run, course_id=job.recording.course_id)

    def _process(self, job: RecordingJob, lecture_id: str, direct_oss: bool = False,
                 regenerate: bool = False, reuse_existing: bool = False,
                 notes_strategy: str | None = None) -> str:
        from ..auth import get_session
        from ..lecture_source_download import prepare_lesson_sources
        from ..media import write_notes
        from ..note_sources import collect_note_sources
        from .transcript_review import clear_regeneration_pending, review_pending
        from ..catalog_snapshots import recording_source_id
        from copy import copy

        metered_settings = self.settings.model_copy() if hasattr(self.settings, 'model_copy') else copy(self.settings)
        if notes_strategy is not None:
            if notes_strategy not in {"transcript", "topics"}:
                raise ValueError("请选择保留讲述顺序或知识点图文笔记。")
            metered_settings.notes_strategy = notes_strategy
        source_id = recording_source_id(job.recording)
        metered_settings._metered_operation_context = {
            'purpose': 'lecture-notes', 'course_id': job.recording.course_id,
            'source_id': source_id, 'client_operation_id': source_id}

        notes_path = job.directory / "notes.md"
        transcript_path = job.directory / "transcript.json"
        regenerate = regenerate or review_pending(job)
        if regenerate and not notes_path.is_file():
            raise ValueError("这节课还没有旧笔记，请使用「整理这节录像」。")
        if regenerate and not transcript_path.is_file():
            raise ValueError("本机没有这节课的转写，无法安全重整旧笔记。")
        if not regenerate and notes_path.exists() and not transcript_path.is_file():
            raise ValueError("本机缺少这节课的转写，无法核对笔记是否覆盖整节录像；请重新整理后再发布。")
        if regenerate:
            # A deliberate rewrite differs from initial generation. Retries
            # keep this identity while the old canonical note is unchanged,
            # so completed paid stages survive publication/network failure.
            metered_settings._metered_operation_context["client_operation_id"] = (
                "notes-" + hashlib.sha256(notes_path.read_bytes()).hexdigest())
        catalog_issues = assignment_catalog_publish_issues(job)
        if catalog_issues:
            raise ValueError("；".join(catalog_issues))
        if not regenerate and not notes_path.exists() and transcript_path.is_file():
            from ..transcript_notes import restore_saved_note
            transcript = json.loads(transcript_path.read_text("utf-8"))
            if restore_saved_note(notes_path, transcript):
                self._stage("恢复已保存的课堂笔记")
        if not regenerate and notes_path.exists():
            # Check saved state before optional media downloads or remote
            # writes. This binds the note to its completed model review;
            # it does not judge its wording, timestamps or citations.
            issues = note_publish_issues(job, notes_path)
            if issues:
                raise ValueError("；".join(issues))
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
                                      staged_note, metered_settings, job.label,
                                      source_context=source_context, progress=self._progress,
                                      course_directory=job.course_dir)
                if written is None:
                    raise ValueError("笔记生成没有完成，请检查 AI 额度后重试。")
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
                    job, metered_settings, include_keyframes=True,
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
                    metered_settings, job.label, source_context=source_context,
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
        if not reuse_existing and not job.video.exists() and needs_video_clips(staged_note or notes_path):
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
        if staged_note is not None:
            from ..note_editor import report_path
            backup = job.directory / f"notes.previous-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.md"
            previous_report = report_path(notes_path)
            staged_report = report_path(staged_note)
            backup_report = report_path(backup)
            report_promoted = False
            try:
                notes_path.replace(backup)
                if previous_report.exists():
                    previous_report.replace(backup_report)
                staged_report.replace(previous_report)
                report_promoted = True
                staged_note.replace(notes_path)
            except Exception:
                if report_promoted:
                    previous_report.replace(staged_report)
                if backup_report.exists():
                    backup_report.replace(previous_report)
                if backup.exists():
                    backup.replace(notes_path)
                raise
        from .notion_home import saved_home
        from ..catalog_snapshots import recording_source_id
        publication = {
            "recording_id": recording_id(job),
            "course_id": job.recording.course_id,
            "source_id": recording_source_id(job.recording),
            "home_id": saved_home(Path(self.settings.data_dir)).get("id", ""),
            "lecture_id": lecture_id,
            "lecture_url": f"https://www.notion.so/{lecture_id.replace('-', '')}",
            "note_url": note_url,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        marker = job.directory / "notion-publication.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        if marker.is_file():
            previous = marker.read_bytes()
            try:
                prior_home = json.loads(previous).get("home_id")
            except (ValueError, AttributeError):
                prior_home = None
            if prior_home != publication["home_id"]:
                backup = marker.with_name("notion-publication.legacy-" + hashlib.sha256(previous).hexdigest()[:16] + ".json")
                if not backup.exists():
                    backup.write_bytes(previous)
        temporary = marker.with_name(marker.name + ".tmp")
        temporary.write_text(json.dumps(publication, ensure_ascii=False, indent=1), "utf-8")
        temporary.replace(marker)
        if regenerate:
            clear_regeneration_pending(job)
        from .note_videos import video_publication_complete
        if (getattr(self.settings, "delete_video_after_processing", False)
                and not (job.directory / ".keep-video").exists()
                and video_publication_complete(job.directory, notes_path=notes_path)):
            job.video.unlink(missing_ok=True)
        with self._lock:
            if self._work:
                self._work.lecture_url = publication["lecture_url"]
        return note_url


def add_recording_routes(app: FastAPI, manager: RecordingWorkManager, *, demo_connection: bool = False) -> None:
    credentials_lock = threading.Lock()
    @app.get("/api/campus/courses")
    def campus_courses_endpoint() -> dict:
        from .campus_catalog import campus_courses
        payload = campus_courses(Path(manager.settings.data_dir))
        # Browsing needs the account-scoped local home identity, not a full
        # remote Notion tree. Every mutation still performs its live checks.
        from .notion_home import saved_home
        home = saved_home(Path(manager.settings.data_dir))
        payload['notion_home'] = ({key: home[key] for key in
            ('id', 'url', 'parent_id', 'schema', 'active') if key in home} if home.get('id') else None)
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
    def notion_parents_endpoint(selected_only: bool = False) -> dict:
        from .notion_home import available_parents, saved_home, legacy_home
        token = getattr(manager.settings, "notion_token", "")
        if not token:
            detail = ("演示模式只模拟 Notion 连接，不会读取真实页面；请退出演示模式后授权 Notion。"
                      if demo_connection else "请先连接 Notion。")
            raise HTTPException(status_code=409, detail=detail)
        try:
            from .notion_workspace import saved_workspace, read_projection, _verify_home, _verify_selected_parent
            root = Path(manager.settings.data_dir)
            home_sync = {'status': 'current'}
            if getattr(manager.settings, 'platform_token', ''):
                from ..platform import _fetch_account_profile
                from .workspace_profile import pull
                profile = _fetch_account_profile(manager.settings)
                if profile.get('status') == 'ready':
                    home_sync = (pull(manager.settings, profile) if profile['profile'].get('notion_workspace_v2')
                                 else {'status': 'absent'})
                else:
                    home_sync = {'status': 'account_unavailable'}
                if home_sync['status'] not in {'pulled', 'current', 'absent'}:
                    pages = (available_parents(token) if home_sync['status'] in
                             {'notion_reconnect_required', 'notion_home_archived', 'notion_home_changed'} else [])
                    return {'pages': pages, 'home': None, 'workspace': None,
                            'legacy_home': legacy_home(root), 'home_sync': home_sync}
            workspace = saved_workspace(root)
            if selected_only and workspace:
                # Routine startup reads the exact configured destination.
                # The explicit picker retains complete search and projection
                # hydration; navigation must not race a writer with a full
                # scan of every course and lecture.
                from ..notion import NotionClient
                from .notion_home import _title
                with NotionClient(token) as client:
                    _verify_home(client.get_page(workspace['id']), workspace['id'], workspace['parent_id'])
                    parent = _verify_selected_parent(client, workspace['parent_id'])
                return {'pages': [{'id': parent['id'], 'title': _title(parent), 'url': parent.get('url', '')}],
                        'home': saved_home(root), 'legacy_home': legacy_home(root), 'workspace': workspace,
                        'home_sync': home_sync}
            if workspace.get("active"):
                read_projection(manager.settings)
            home = saved_home(Path(manager.settings.data_dir))
            from .notion_workspace import saved_workspace
            from .notion_home import legacy_home
            return {"pages": available_parents(token), "home": home,
                    "legacy_home": legacy_home(Path(manager.settings.data_dir)),
                    "workspace": saved_workspace(Path(manager.settings.data_dir)), 'home_sync': home_sync}
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
        from .notion_home import move_home, saved_home
        token = getattr(manager.settings, "notion_token", "")
        if not token:
            raise HTTPException(status_code=409, detail="请先连接 Notion。")
        try:
            root = Path(manager.settings.data_dir)
            home = (move_home(root, token, body.parent_page_id, settings=manager.settings)
                    if saved_home(root).get("schema") == 2 else move_home(root, token, body.parent_page_id))
            from ..platform import publish_account_profile
            home["profile_sync"] = ({"available": True, "status": "pushed", "revision": home["profile_revision"]}
                                    if home.get("schema") == 2 else publish_account_profile(manager.settings))
            return home
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Notion learning home move failed")
            raise HTTPException(status_code=502, detail="学习主页没有移动成功，请检查 Notion 页面权限后重试。") from exc

    @app.post("/api/campus/notion/workspace")
    def notion_workspace_endpoint(body: HomeRequest) -> dict:
        from .notion_workspace import sync_workspace
        if manager.state().get("state") == "running":
            raise HTTPException(status_code=409, detail="请等待当前任务完成，再同步新版主页。")
        try:
            options = {'replace_existing': True} if body.replace_existing else {}
            return sync_workspace(manager.settings, body.parent_page_id, **options)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Notion version-two workspace sync failed")
            raise HTTPException(status_code=502, detail="新版主页未完成验证，请稍后重试；旧主页已保留。") from exc

    @app.get("/api/recordings")
    def recordings() -> dict:
        return {"items": list_recordings(Path(manager.settings.data_dir)),
                "campus_configured": bool(getattr(manager.settings, "pku_username", "") and getattr(manager.settings, "pku_password", ""))}

    @app.post("/api/recordings/{recording_key}/summary-acceptance")
    def summary_acceptance_endpoint(recording_key: str, body: SummaryAcceptanceRequest) -> dict:
        from .summary_acceptance import accept_published
        from ..notion import NotionClient
        job = find_job(Path(manager.settings.data_dir), recording_key)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到对应教学网讲次。")
        if manager.state().get("state") == "running":
            raise HTTPException(status_code=409, detail="请等待当前任务完成后审阅总结。")
        try:
            with NotionClient(manager.settings.notion_token) as client:
                return accept_published(job, client=client, notes_sha256=body.notes_sha256, confirmed=body.confirmed)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Notion summary acceptance verification failed")
            raise HTTPException(status_code=502, detail="Notion 验收未能验证，请稍后重试。") from exc

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
        from ..auth import CampusLoginRejected, get_session

        username = body.username.strip()
        if not username:
            raise HTTPException(status_code=400, detail="请输入教学网账号。")
        if not credentials_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="正在验证教学网账号，请等待本次验证完成。")
        try:
            binding = manager._account_binding()
            current = manager.state()
            if current.get("state") == "running" or current.get("background_sync", {}).get("state") == "running":
                raise HTTPException(status_code=409, detail="请等待当前课程任务结束后再更换教学网账号。")
            try:
                client = get_session(username=username, password=body.password)
                client.close()
            except CampusLoginRejected as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from None
            except Exception as exc:
                # Upstream bodies/URLs may contain credentials or OAuth tokens.
                logger.warning("campus credential verification failed type=%s", type(exc).__name__)
                raise HTTPException(status_code=503, detail="暂时无法连接教学网验证账号，请检查网络后重试；这次填写的账号尚未保存。") from None
            with manager._lock:
                if binding != manager._account_binding():
                    raise HTTPException(status_code=409, detail="账号或连接已改变，请重新验证教学网账号。")
                current = manager.state()
                if current.get("state") == "running" or current.get("background_sync", {}).get("state") == "running":
                    raise HTTPException(status_code=409, detail="课程任务已开始，请等待任务结束后再保存账号。")
                try:
                    write_env_values(env_file_path(manager.settings),
                                     {"PKU_USERNAME": username, "PKU_PASSWORD": body.password})
                except OSError:
                    raise HTTPException(status_code=503, detail="账号验证成功，但本机配置无法保存，请检查磁盘空间和写入权限后重试。") from None
                manager.settings.pku_username = username
                manager.settings.pku_password = body.password
            return {"configured": True, "verified": True}
        finally:
            credentials_lock.release()

    @app.post("/api/recordings/sync")
    def sync(body: CourseRefreshRequest | None = None) -> dict:
        try:
            course_id = body.course_id.strip() if body else ""
            if body and body.scope not in {"", "background"}:
                raise ValueError("未知课程更新范围。")
            if course_id and body and (body.resume or body.scope):
                raise ValueError("单课更新与后台恢复不能混用。")
            if course_id:
                return manager.start_sync(course_id=course_id)
            return manager.start_background_sync(resume=body.resume if body else False)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/recordings/cancel-sync")
    def cancel_sync(body: CourseRefreshRequest | None = None) -> dict:
        try:
            if body and body.scope == "background":
                return manager.cancel_background_sync()
            if body and (body.scope or body.course_id or body.resume):
                raise ValueError("未知取消范围。")
            # Legacy no-body cancellation still finds a sole background sync.
            if manager.state()["state"] != "running" and manager.background_state()["state"] == "running":
                return manager.cancel_background_sync()
            return manager.cancel_sync()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/recordings/task")
    async def task(scope: str = "") -> dict:
        # This is a bounded in-memory snapshot. Keep progress/close observers
        # independent of slow file or service reads occupying HTTP workers.
        # The manager still returns the actual state under its existing lock.
        if scope == "background":
            return manager.background_state()
        if scope:
            raise HTTPException(status_code=400, detail="未知任务范围。")
        return manager.state()

    @app.post("/api/campus/notion/sync")
    def notion_catalog_sync(body: CourseRefreshRequest | None = None) -> dict:
        try:
            if body and body.scope not in {"", "background"}:
                raise ValueError("未知课程更新范围。")
            if body and body.course_id and (body.scope or body.resume):
                raise ValueError("单课更新与后台恢复不能混用。")
            if body and body.course_id:
                return manager.start_sync(body.course_id.strip())
            return manager.start_background_sync(resume=body.resume if body else False)
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
                reuse_existing=body.reuse_existing if body else False,
                **({"notes_strategy":body.notes_strategy} if body and body.notes_strategy is not None else {}))
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
                                         direct_oss=body.direct_oss, regenerate=body.regenerate,
                                         **({"notes_strategy":body.notes_strategy} if body.notes_strategy is not None else {}))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
