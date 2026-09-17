"""Read-only local tools exposed by the pku-sync MCP server.

The server deliberately does not expose arbitrary filesystem access or invoke
the expensive sync/transcription pipeline. The system scheduler runs those
deterministic stages first; an MCP-capable agent then reads the resulting
course tree here and writes Notion through Notion's official MCP server.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from .pipeline import collect_jobs, recording_stage

TextSection = Literal["all", "assignments", "announcements", "materials", "recordings", "logs"]
_TEXT_SUFFIXES = {".json", ".log", ".md", ".txt"}
_MAX_READ_CHARS = 200_000


class LocalCourseTools:
    """Safe, deterministic view over one configured ``DATA_DIR``."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.expanduser().resolve()

    def health(self) -> dict:
        return {
            "ok": self.data_dir.is_dir(),
            "data_dir": str(self.data_dir),
            "courses": len(self.list_courses()),
        }

    def list_courses(self) -> list[dict]:
        courses: list[dict] = []
        if not self.data_dir.is_dir():
            return courses
        for meta in sorted(self.data_dir.glob("*/course.json")):
            try:
                item = json.loads(meta.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                item = {}
            courses.append(
                {
                    "folder": meta.parent.name,
                    "course_id": item.get("course_id", ""),
                    "name": item.get("name") or meta.parent.name,
                    "code": item.get("code", ""),
                    "synced_at": item.get("synced_at", ""),
                }
            )
        return courses

    def list_files(
        self,
        course_folder: str = "",
        section: TextSection = "all",
    ) -> list[dict]:
        """List relevant local artifacts, returning paths relative to DATA_DIR."""
        base = self._course_dir(course_folder) if course_folder else self.data_dir
        patterns = _section_patterns(section)
        files: dict[str, dict] = {}
        for pattern in patterns:
            effective = (
                pattern
                if course_folder or pattern.startswith("logs/")
                else f"*/{pattern}"
            )
            for path in base.glob(effective):
                if not path.is_file():
                    continue
                relative = path.relative_to(self.data_dir).as_posix()
                stat = path.stat()
                files[relative] = {
                    "path": relative,
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                }
        return [files[key] for key in sorted(files)]

    def read_text(self, relative_path: str, max_chars: int = 50_000) -> dict:
        """Read one text artifact below DATA_DIR; traversal and binary files fail."""
        if max_chars < 1 or max_chars > _MAX_READ_CHARS:
            raise ValueError(f"max_chars must be between 1 and {_MAX_READ_CHARS}")
        path = self._resolve(relative_path)
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            raise ValueError(f"unsupported text file type: {path.suffix or '(none)'}")
        text = path.read_text("utf-8", errors="replace")
        return {
            "path": path.relative_to(self.data_dir).as_posix(),
            "content": text[:max_chars],
            "truncated": len(text) > max_chars,
            "total_chars": len(text),
        }

    def recording_status(self, course_id: str = "") -> list[dict]:
        rows: list[dict] = []
        for job in collect_jobs(self.data_dir, course_id):
            status, unavailable = recording_stage(job)
            notes = job.directory / "notes.md"
            rows.append(
                {
                    "course": job.course_name,
                    "course_id": job.recording.course_id,
                    "title": job.recording.title,
                    "recorded_at": job.recording.recorded_at,
                    "status": status,
                    "unavailable_reason": unavailable,
                    "directory": job.directory.relative_to(self.data_dir).as_posix(),
                    "notes_chars": notes.stat().st_size if notes.exists() else 0,
                    "keyframes": len(list((job.directory / "keyframes").glob("*"))),
                }
            )
        return rows

    def daily_status(self, max_chars: int = 30_000) -> dict:
        logs = self.data_dir / "logs"
        latest = logs / "latest.daily.log"
        summaries = sorted(logs.glob("summary_*.md"), key=lambda p: p.stat().st_mtime)
        result: dict[str, object] = {
            "latest_log": None,
            "latest_summary": None,
        }
        if latest.exists():
            result["latest_log"] = self.read_text(
                latest.relative_to(self.data_dir).as_posix(), max_chars
            )
        if summaries:
            result["latest_summary"] = self.read_text(
                summaries[-1].relative_to(self.data_dir).as_posix(), max_chars
            )
        return result

    def _course_dir(self, course_folder: str) -> Path:
        if not course_folder or "/" in course_folder or "\\" in course_folder:
            raise ValueError("course_folder must be one direct child directory name")
        path = self._resolve(course_folder)
        if not path.is_dir():
            raise FileNotFoundError(f"course folder not found: {course_folder}")
        return path

    def _resolve(self, relative_path: str) -> Path:
        raw = Path(relative_path)
        if not relative_path or raw.is_absolute():
            raise ValueError("path must be relative to DATA_DIR")
        path = (self.data_dir / raw).resolve()
        if not path.is_relative_to(self.data_dir):
            raise ValueError("path escapes DATA_DIR")
        if not path.exists():
            raise FileNotFoundError(f"path not found: {relative_path}")
        return path


def _section_patterns(section: TextSection) -> tuple[str, ...]:
    patterns = {
        "assignments": ("assignments.md", "deadlines.json"),
        "announcements": ("announcements/*.md",),
        "materials": ("materials/**/*",),
        "recordings": (
            "recordings/index.json",
            "recordings/*/recording.json",
            "recordings/*/transcript.json",
            "recordings/*/notes.md",
        ),
        "logs": ("logs/latest.daily.log", "logs/summary_*.md"),
    }
    if section == "all":
        return tuple(pattern for values in patterns.values() for pattern in values)
    try:
        return patterns[section]
    except KeyError as exc:
        raise ValueError(f"unknown section: {section}") from exc
