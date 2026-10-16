"""Read-only local tools exposed by the pku-sync MCP server.

The server deliberately does not expose arbitrary filesystem access or invoke
the expensive sync/transcription pipeline. The system scheduler runs those
deterministic stages first; MCP-capable agents then read the resulting local
course tree here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from .pipeline import collect_jobs, recording_stage

TextSection = Literal["all", "assignments", "announcements", "materials", "recordings"]
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
            if not isinstance(item, dict):
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
                if course_folder
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

    def _matching_courses(self, course: str) -> list[dict]:
        rows = self.list_courses()
        if not course.strip():
            return rows
        query = course.strip().casefold()
        exact = [row for row in rows if any(query == str(row[key]).casefold()
                 for key in ('course_id', 'folder', 'code', 'name'))]
        return exact or [row for row in rows if query in row['name'].casefold()]

    def get_lecture_notes(self, course: str, lecture: str = "") -> list[dict]:
        """Read generated lecture notes, optionally filtered by title or date."""
        rows = []
        for item in self._matching_courses(course):
            folder = self._course_dir(item['folder'])
            jobs = {job.directory.resolve(): job for job in collect_jobs(self.data_dir, item['course_id'])
                    if job.course_dir.resolve() == folder}
            for path in sorted(folder.glob('recordings/*/notes.md')):
                path = self._resolve(path.relative_to(self.data_dir).as_posix())
                job = jobs.get(path.parent)
                title, day = path.parent.name, ''
                if job:
                    title, day = job.recording.title, job.recording.date
                else:
                    try:
                        meta = json.loads((path.parent / 'recording.json').read_text('utf-8'))
                        if isinstance(meta, dict):
                            title = str(meta.get('title') or title)
                            day = str(meta.get('recorded_at') or '')[:10]
                    except (OSError, ValueError):
                        pass
                if lecture.casefold() not in (title + ' ' + day).casefold():
                    continue
                try:
                    markdown = path.read_text('utf-8', errors='replace')
                except OSError:
                    continue
                if markdown.strip():
                    rows.append({'course': item['name'], 'course_id': item['course_id'],
                                 'title': title, 'date': day, 'notes_markdown': markdown})
        return rows

    def get_course_assignments(self, course_folder: str) -> list[dict]:
        """Read the selected course's local assignment index."""
        folder = self._course_dir(course_folder)
        path = self._resolve((folder / 'assignments/index.json').relative_to(self.data_dir).as_posix())
        assignments = json.loads(path.read_text('utf-8'))
        if not isinstance(assignments, list):
            raise ValueError('assignment index must be a list')
        return [item for item in assignments if isinstance(item, dict)]

    def get_course_summary(self, course: str = "") -> dict:
        """Read course digests, lecture coverage and local assignment deadlines."""
        from .panel.course_summary import summary_markdown
        rows = []
        for item in self._matching_courses(course):
            notes = self.get_lecture_notes(item['folder'])
            jobs = [job for job in collect_jobs(self.data_dir, item['course_id'])
                    if job.course_dir.name == item['folder']]
            try:
                markdown = summary_markdown(self.data_dir, item['course_id']) if item['course_id'] else ''
            except (ValueError, OSError):
                markdown = ''
            if not markdown:
                markdown = '# ' + item['name'] + '\n\n' + '\n\n'.join(
                    '## ' + row['title'] + '\n' + row['notes_markdown'] for row in notes)
            try:
                assignments = self.get_course_assignments(item['folder'])
            except (OSError, ValueError):
                assignments = []
            rows.append({**item, 'markdown': markdown, 'total_lessons': max(len(jobs), len(notes)),
                         'summarized_lessons': len(notes), 'assignments_count': len(assignments),
                         'assignments': [{'title': a.get('title', ''),
                                          'due_at': a.get('due_at') or a.get('due_at_label') or ''}
                                         for a in assignments if isinstance(a, dict)]})
        return rows[0] if course and len(rows) == 1 else {'courses': rows}

    def search_course_knowledge(self, query: str, course: str = "") -> list[dict]:
        """Rank local summary, note, assignment, announcement, material and transcript excerpts."""
        tokens = [token for token in re.split(r"[\s,，、。；;：:！!？?/|]+", query.casefold()) if token]
        if not tokens:
            return []
        hits = []
        patterns = [
            ('summary', 'course-summary.md'),
            ('notes', 'recordings/*/notes.md'),
            ('assignments', 'assignments/index.json'),
            ('announcements', 'announcements/index.json'),
            ('materials', 'materials/index.json'),
            ('transcript', 'recordings/*/transcript.json'),
        ]
        for item in self._matching_courses(course):
            folder = self._course_dir(item['folder'])
            jobs = {job.directory.resolve(): job for job in collect_jobs(self.data_dir, item['course_id'])
                    if job.course_dir.resolve() == folder}
            for kind, pattern in patterns:
                for path in sorted(folder.glob(pattern)):
                    try:
                        content = self.read_text(path.relative_to(self.data_dir).as_posix(), _MAX_READ_CHARS)['content']
                        if path.suffix == '.json':
                            value = json.loads(content)
                            # Search student-facing text, never private media URLs or credentials.
                            def prose(value):
                                if isinstance(value, list):
                                    return '\n'.join(prose(v) for v in value)
                                if isinstance(value, dict):
                                    return '\n'.join(prose(v) for k, v in value.items() if k in
                                        {'title', 'text', 'body', 'body_text', 'instructions', 'content', 'segments', 'due_at', 'files', 'category'})
                                return str(value or '')
                            content = prose(value)
                    except (OSError, ValueError):
                        continue
                    lowered = content.casefold()
                    score = sum(lowered.count(token) for token in tokens)
                    if not score:
                        continue
                    offset = min(lowered.find(token) for token in tokens if token in lowered)
                    title, day = item['name'], ''
                    if kind == 'summary':
                        title = f"{item['name']} 课程总结"
                    if kind in {'notes', 'transcript'}:
                        title = path.parent.name
                        job = jobs.get(path.parent.resolve())
                        if job:
                            title, day = job.recording.title, job.recording.date
                        else:
                            try:
                                meta = json.loads((path.parent / 'recording.json').read_text('utf-8'))
                                if isinstance(meta, dict):
                                    title = str(meta.get('title') or title)
                                    day = str(meta.get('recorded_at') or '')[:10]
                            except (OSError, ValueError):
                                pass
                    hit = {'course': item['name'], 'source_type': kind, 'title': title,
                           'snippet': content[max(0, offset - 100):offset + 400], '_score': score}
                    if day:
                        hit['date'] = day
                    hits.append(hit)
        priority = {'summary': 0, 'notes': 0, 'assignments': 1, 'announcements': 1, 'materials': 1, 'transcript': 2}
        hits.sort(key=lambda row: (-row['_score'], priority[row['source_type']]))
        return [{k: v for k, v in row.items() if k != '_score'} for row in hits[:20]]

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
    }
    if section == "all":
        return tuple(pattern for values in patterns.values() for pattern in values)
    try:
        return patterns[section]
    except KeyError as exc:
        raise ValueError(f"unknown section: {section}") from exc
