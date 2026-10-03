"""Stable course-scoped recording source selection for the student panel."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from ..pipeline import collect_jobs, recording_stage
from .recordings_api import recording_id


def _key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\W_]+", "", normalized)


def _course(directory_service, course_id: str) -> dict:
    try:
        directory = directory_service.ensure_loaded()
    except Exception as exc:
        raise ValueError("无法读取课程目录，请先重新连接或同步。") from exc
    for course in directory.courses:
        if course.id == course_id:
            return {"id": course.id, "title": course.title}
    raise ValueError("找不到该课程，请先刷新学习空间。")


def _campus_courses(data_dir: Path) -> list[dict]:
    courses = []
    for path in sorted(data_dir.glob("*/course.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        course_id, name = data.get("course_id"), data.get("name")
        if isinstance(course_id, str) and course_id and isinstance(name, str) and name:
            courses.append({"id": course_id, "title": name})
    return courses


def _mapping_path(data_dir: Path) -> Path:
    return data_dir / "recording-course-map.json"


def _saved_mapping(data_dir: Path) -> dict[str, str]:
    try:
        value = json.loads(_mapping_path(data_dir).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        key: item for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    } if isinstance(value, dict) else {}


def source_course(data_dir: Path, directory_service, notion_course_id: str) -> tuple[dict, str, list[dict]]:
    course = _course(directory_service, notion_course_id)
    candidates = _campus_courses(data_dir)
    ids = {item["id"] for item in candidates}
    saved = _saved_mapping(data_dir).get(notion_course_id)
    if saved in ids:
        return course, saved, candidates
    matches = [item["id"] for item in candidates if _key(item["title"]) == _key(course.get("title", ""))]
    return course, matches[0] if len(matches) == 1 else "", candidates


def choose_source(data_dir: Path, directory_service, notion_course_id: str, campus_course_id: str) -> None:
    _, _, candidates = source_course(data_dir, directory_service, notion_course_id)
    if campus_course_id not in {item["id"] for item in candidates}:
        raise ValueError("请选择已同步的教学网课程。")
    path = _mapping_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    mapping = _saved_mapping(data_dir)
    mapping[notion_course_id] = campus_course_id
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(mapping, ensure_ascii=False, indent=1), "utf-8")
    temporary.replace(path)


def rows_for_course(data_dir: Path, directory_service, notion_course_id: str) -> dict:
    _, selected, candidates = source_course(data_dir, directory_service, notion_course_id)
    items = []
    if selected:
        for job in collect_jobs(data_dir, selected):
            stage, unavailable = recording_stage(job)
            items.append({
                "id": recording_id(job),
                "title": job.recording.title,
                "date": job.recording.date,
                "stage": stage,
                "video_available": job.video.exists(),
                "unavailable_reason": unavailable,
            })
    return {
        "items": items,
        "campus_courses": candidates,
        "selected_campus_course_id": selected,
    }
