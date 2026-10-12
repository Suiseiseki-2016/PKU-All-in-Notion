"""Choose local projection folders by native course ID without moving files."""
from __future__ import annotations

import json
from pathlib import Path

from .store import safe_name


class CourseStorageConflict(ValueError):
    pass


def _inside(root: Path, path: Path) -> bool:
    target, base = path.resolve(), root.resolve()
    return target != base and target.is_relative_to(base)


def plan_course_directories(root: Path, courses) -> dict[str, Path]:
    """Validate every selected course before any projection/cache mutation."""
    root = Path(root)
    existing: dict[str, list[Path]] = {}
    for marker in sorted(root.glob("*/course.json")):
        if not _inside(root, marker.parent) or not _inside(root, marker):
            continue
        try:
            if marker.stat().st_size > 256_000:
                continue
            metadata = json.loads(marker.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        identifier = metadata.get("course_id") if isinstance(metadata, dict) else None
        if isinstance(identifier, str) and identifier:
            existing.setdefault(identifier, []).append(marker.parent)

    result = {}
    reserved = set()
    for course in courses:
        identifier = course.course_id
        if identifier in result:
            raise CourseStorageConflict("教学网课程身份重复，未选择写入目录。")
        matches = existing.get(identifier, [])
        if len(matches) > 1:
            raise CourseStorageConflict("同一教学网课程对应多个本地目录，请先核对；所有目录均已保留。")
        if matches:
            chosen = matches[0]
        else:
            names = [safe_name(course.directory, identifier.strip("_")),
                     safe_name("course_" + identifier + "_" + course.directory, "course")]
            chosen = None
            for name in names:
                candidate = root / name
                if not _inside(root, candidate) or candidate.resolve() in reserved:
                    continue
                try:
                    unclaimed = not candidate.exists() or (candidate.is_dir() and not any(candidate.iterdir()))
                except OSError:
                    unclaimed = False
                if unclaimed:
                    chosen = candidate
                    break
            if chosen is None:
                raise CourseStorageConflict("课程目录已被占用或身份无法核对；已有文件均已保留。")
        if chosen.resolve() in reserved:
            raise CourseStorageConflict("多门课程对应同一目录，未执行写入。")
        reserved.add(chosen.resolve())
        result[identifier] = chosen
    return result


def course_directory(root: Path, course) -> Path:
    return plan_course_directories(root, [course])[course.course_id]
