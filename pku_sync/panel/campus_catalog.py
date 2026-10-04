"""Campus-first, metadata-only course catalog for the student panel.

The course and lecture order comes from the Teaching Network recording index.
Attachments and video addresses never leave the local data directory here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..pipeline import collect_jobs, recording_stage
from .recordings_api import recording_id, _safe_attachment_path
from .campus_todos import distinct_announcement_tasks, announcement_source_url
from .handbook_requirements import handbook_review_cards

_LECTURE_NUMBER = re.compile(r"第\s*([0-9０-９]+)\s*[讲次]", re.IGNORECASE)
_TEACHER_DEADLINE = re.compile(
    r"(?:DDL|截止(?:时间|日期)?|最晚(?:提交)?)(?:\s|[:：]|为|是){0,10}"
    r"(?P<date>20\d{2}[/-]\d{1,2}[/-]\d{1,2}"
    r"(?:（[^）]{1,8}）|\([^)]{1,8}\))?\s*\d{1,2}[:：]\d{2})",
    re.I,
)


def display_campus_time(value: str) -> str:
    """Show timezone-aware campus timestamps in PKU's local time."""
    raw = str(value or "")
    if not raw:
        return ""
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if moment.tzinfo is None:
        return raw
    beijing = timezone(timedelta(hours=8))
    return moment.astimezone(beijing).strftime("%Y-%m-%d %H:%M 北京时间")


def teacher_deadline_quote(instructions: str) -> str:
    """Extract one explicit teacher-written deadline, without assigning a timezone.

    This is a display clue when the Teaching Network's structured due field is
    empty. Conflicting dates or invalid calendar values must stay in the
    original instructions for the student to resolve.
    """
    candidates: set[str] = set()
    for match in _TEACHER_DEADLINE.finditer(str(instructions or "")):
        raw = match.group("date").strip()
        date = re.match(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", raw)
        clock = re.search(r"(\d{1,2})[:：](\d{2})$", raw)
        if not date or not clock:
            continue
        try:
            datetime(*(int(value) for value in (*date.groups(), *clock.groups())))
        except ValueError:
            continue
        candidates.add(raw)
    return next(iter(candidates)) if len(candidates) == 1 else ""


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _number(text: str) -> int | None:
    match = _LECTURE_NUMBER.search(text or "")
    return int(match.group(1)) if match else None


def _courses(root: Path):
    for path in sorted(root.glob("*/course.json")):
        metadata = _read_json(path, {})
        if not isinstance(metadata, dict):
            continue
        course_id = metadata.get("course_id")
        title = metadata.get("name")
        if isinstance(course_id, str) and course_id and isinstance(title, str) and title:
            yield path.parent, course_id, title, metadata


def _category(item: dict) -> str:
    kind = str(item.get("kind") or "")
    title = str(item.get("title") or "")
    if kind in {"作业", "考试", "测试"} or any(word in title for word in ("作业", "测验", "考试", "随堂测试")):
        return "assignment"
    if item.get("files") or kind == "文件":
        return "material"
    return "information"


def campus_courses(root: Path) -> dict:
    rows = []
    for folder, course_id, title, metadata in _courses(root):
        materials = _read_json(folder / "materials" / "index.json", [])
        notices = _read_json(folder / "announcements" / "index.json", [])
        assignments = _read_json(folder / "assignments" / "index.json", None)
        handbooks = handbook_review_cards(folder / "materials", materials) if isinstance(materials, list) else []
        rows.append({
            "id": course_id,
            "title": title,
            "code": str(metadata.get("code") or ""),
            "term": str(metadata.get("term") or ""),
            "recordings": len(collect_jobs(root, course_id)),
            "materials": sum(1 for item in materials if isinstance(item, dict)
                             and _category(item) == "material") if isinstance(materials, list) else 0,
            "assignments": len(assignments) if isinstance(assignments, list) else
                sum(1 for item in materials if isinstance(item, dict)
                    and _category(item) == "assignment") if isinstance(materials, list) else 0,
            "handbook_reviews": len(handbooks),
            "announcements": len(notices) if isinstance(notices, list) else 0,
            "announcement_tasks": len(distinct_announcement_tasks(course_id, notices, assignments if isinstance(assignments, list) else [])) if isinstance(notices, list) else 0,
            "information": sum(1 for item in materials if isinstance(item, dict)
                               and _category(item) == "information") if isinstance(materials, list) else 0,
            "synced_at": str(metadata.get("synced_at") or ""),
        })
    return {"courses": rows}


def campus_course(root: Path, course_id: str) -> dict:
    found = next(((folder, title, meta) for folder, key, title, meta in _courses(root)
                  if key == course_id), None)
    if found is None:
        raise ValueError("教学网课程尚未同步，请先刷新课程目录。")
    folder, title, metadata = found
    jobs = sorted(collect_jobs(root, course_id),
                  key=lambda job: (job.recording.recorded_at, job.recording.title))
    lessons = []
    home = _read_json(root / "notion-learning-home.json", {})
    home_id = home.get("id") if isinstance(home, dict) else None
    by_number: dict[int, list[dict]] = {}
    for job in jobs:
        stage, unavailable = recording_stage(job)
        from .transcript_review import review_pending
        row = {
            "id": recording_id(job),
            "title": job.recording.title,
            "date": job.recording.date,
            "number": _number(job.recording.title),
            "stage": stage,
            "video_available": job.video.exists(),
            "transcript_available": (job.directory / "transcript.json").is_file(),
            "notes_available": (job.directory / "notes.md").is_file(),
            "keyframes_available": (job.directory / "keyframes" / "index.json").is_file(),
            "transcript_reviewed_pending": review_pending(job),
            "duration_seconds": (
                _read_json(job.directory / "duration.json", {}).get("duration_seconds")
                or _read_json(job.directory / "recording.json", {}).get("duration_seconds")
                or job.recording.duration_seconds
            ),
            "video_size": job.video.stat().st_size if job.video.exists() else None,
            "unavailable_reason": unavailable,
            "materials": [],
        }
        row["publish_ready"] = bool(
            row["transcript_available"]
            and row["notes_available"]
            and row["keyframes_available"]
        )
        publication = _read_json(job.directory / "notion-publication.json", {})
        if (isinstance(publication, dict)
                and publication.get("recording_id") == row["id"]
                and publication.get("home_id") == home_id
                and publication.get("note_url")
                and publication.get("lecture_url")):
            row["notion_note_url"] = publication["note_url"]
            row["notion_lecture_url"] = publication["lecture_url"]
        lessons.append(row)
        if row["number"] is not None:
            by_number.setdefault(row["number"], []).append(row)
    raw_materials = _read_json(folder / "materials" / "index.json", [])
    notices = _read_json(folder / "announcements" / "index.json", [])
    assignments = _read_json(folder / "assignments" / "index.json", [])
    handbook_reviews = handbook_review_cards(folder / "materials", raw_materials) if isinstance(raw_materials, list) else []
    unassigned = []
    all_materials = []
    overrides = _read_json(folder / "materials" / "matches.json", {})
    if not isinstance(overrides, dict):
        overrides = {}
    if isinstance(raw_materials, list):
        for item in raw_materials:
            if not isinstance(item, dict):
                continue
            material = {
                "title": str(item.get("title") or "未命名课件"),
                "content_id": str(item.get("content_id") or ""),
                "kind": str(item.get("kind") or "资料"),
                "path": str(item.get("path") or ""),
                "files": [str(name) for name in item.get("files", []) if isinstance(name, str)],
                "attachments": [{"filename": str(asset.get("filename") or "附件"),
                                 "path": path}
                                for asset in item.get("attachments", [])
                                if isinstance(asset, dict)
                                if (path := _safe_attachment_path(asset.get("path")))],
                "parent_content_id": str(item.get("parent_content_id") or ""),
                "category": _category(item),
                "index": len(all_materials),
            }
            all_materials.append(material)
            override = overrides.get(material["path"])
            if override == "ignore":
                material["match_state"] = "ignored"
                unassigned.append(material)
                continue
            if isinstance(override, str) and override:
                match = next((lesson for lesson in lessons if lesson["id"] == override), None)
                if match:
                    material["match_state"] = "confirmed"
                    match["materials"].append(material)
                    continue
            number = _number(material["path"] + " " + material["title"])
            matches = by_number.get(number, []) if number is not None else []
            if len(matches) == 1:
                matches[0]["materials"].append(material)
            else:
                material["match_state"] = "needs_review"
                unassigned.append(material)
    # The formal assignment index intentionally stores only filenames.  Its
    # corresponding content-tree material retains stable, login-protected
    # WebDAV paths. Match by Blackboard content ID, never by a similar title.
    assignment_materials = {
        item["content_id"]: item for item in all_materials
        if item["category"] == "assignment" and item["content_id"]
    }
    formal_assignments = []
    if isinstance(assignments, list):
        for item in assignments:
            if not isinstance(item, dict):
                continue
            row = {**item, "due_at_label": display_campus_time(item.get("due_at")),
                   "teacher_deadline_quote": teacher_deadline_quote(item.get("instructions"))}
            if item.get("source") == "content-tree":
                material = assignment_materials.get(str(item.get("content_id") or ""))
                row["attachments"] = material["attachments"] if material else []
            formal_assignments.append(row)
    return {
        "id": course_id,
        "title": title,
        "code": str(metadata.get("code") or ""),
        "term": str(metadata.get("term") or ""),
        "lessons": lessons,
        "unassigned_materials": unassigned,
        "all_materials": all_materials,
        "announcements": [{**item, "source_url": announcement_source_url(course_id, str(item.get("id") or "")), "posted_at_label": display_campus_time(item.get("posted_at"))}
                          for item in notices if isinstance(item, dict)] if isinstance(notices, list) else [],
        "announcement_tasks": distinct_announcement_tasks(course_id, notices, assignments if isinstance(assignments, list) else []) if isinstance(notices, list) else [],
        "handbook_reviews": handbook_reviews,
        "announcements_status": str(metadata.get("announcements_status") or "not_synced"),
        "assignments_status": str(metadata.get("assignments_status") or "not_synced"),
        "assignments": formal_assignments,
        "synced_at": str(metadata.get("synced_at") or ""),
        "synced_at_label": display_campus_time(metadata.get("synced_at")),
        "existing_notion_url": str(metadata.get("existing_notion_url") or ""),
        "summary": _read_json(folder / "summary.json", {}),
        "source_priority": ["recordings", "materials"],
    }
