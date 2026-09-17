"""Pure data layer for the TUI: local health and live Teaching Network data.

No textual import here on purpose: everything in this module is plain
functions and dataclasses, so the TUI's data half is unit-testable without a
terminal, and the CLI can reuse it outside the App.

The pipeline-health source remains below ``DATA_DIR``:
  - ``logs/latest.daily.log`` — written by ``pku-sync daily``; carries the
    ``=== pku-sync daily started <stamp> ===`` header and the
    ``=== EXIT_CODE=<n> ===`` tail that both the MCP review and this TUI read.
  - ``logs/review_YYYYMMDD.md`` / ``logs/lecture_YYYYMMDD.md`` — agent-step
    reports written by agent_runner.

Course, assignment and recording data is different: ``fetch_live_snapshot``
authenticates to Blackboard on every refresh and parses the website directly.
It deliberately does not fall back to the local course tree, because stale
files must never look like a successful live refresh.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import httpx

    from .config import Settings
    from .models import Assignment, ContentItem, Course, Recording

_START_RE = re.compile(r"=== pku-sync daily started (\S+) ===")
_EXIT_RE = re.compile(r"=== EXIT_CODE=(\d+) ===")
_TABLE_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$")
_ISO_FRACTION_RE = re.compile(r"\.(\d{7,})")
_UNDATED = ("未公布", "待确认", "无", "-")


@dataclass(frozen=True)
class DailyRunStatus:
    """What the latest daily log says about the pipeline."""

    started: str | None  # stamp from the "started" banner, e.g. 20260916_060002
    exit_code: int | None  # EXIT_CODE tail; None = marker missing (log truncated?)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class Deadline:
    course: str
    title: str
    due_at: str  # raw text as stored ("未公布" when the site has no date)
    source: str = ""
    due: datetime | None = None  # parsed date; None = undated

    @property
    def dated(self) -> bool:
        return self.due is not None


@dataclass(frozen=True)
class LiveCourseStatus:
    """Website-native counts for one selected Blackboard course."""

    course: str
    course_id: str
    assignments: int
    recordings: int
    nearest_due: str = ""
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiveSnapshot:
    """One read-only view of the Teaching Network at ``fetched_at``."""

    fetched_at: datetime
    courses: tuple[LiveCourseStatus, ...]
    deadlines: tuple[Deadline, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalRecordingStatus:
    """One local recording and its deterministic processing stage."""

    title: str
    recorded_at: str
    status: str
    directory: str
    notes_chars: int = 0
    keyframes: int = 0
    unavailable_reason: str = ""


@dataclass(frozen=True)
class LocalCourseStatus:
    """Course materials and recording state already present on disk."""

    folder: str
    course_id: str
    name: str
    material_files: tuple[str, ...] = ()
    announcement_files: tuple[str, ...] = ()
    assignment_count: int = 0
    recordings: tuple[LocalRecordingStatus, ...] = ()

    @property
    def recordings_ready(self) -> int:
        return sum(recording.status == "notes_ready" for recording in self.recordings)

    @property
    def recordings_pending(self) -> int:
        return sum(
            recording.status in {"indexed", "downloaded", "transcribed"}
            for recording in self.recordings
        )

    @property
    def recordings_unavailable(self) -> int:
        return sum(recording.status == "unavailable" for recording in self.recordings)


@dataclass(frozen=True)
class NotionRunStatus:
    """Local report-backed view of the latest Notion agent runs."""

    review_report: str | None = None
    lecture_report: str | None = None
    review_excerpt: str = ""
    lecture_excerpt: str = ""


@dataclass(frozen=True)
class LocalWorkbenchSnapshot:
    """Fast local view used by the TUI before any network fetch."""

    courses: tuple[LocalCourseStatus, ...]
    notion: NotionRunStatus


@dataclass(frozen=True)
class CourseArtifact:
    """A concise, path-backed item shown in the course-detail table."""

    kind: str
    title: str
    summary: str
    path: str
    recording_directory: str = ""

    @property
    def selectable_recording(self) -> bool:
        return bool(self.recording_directory)


def fetch_live_snapshot(
    config: Settings | None = None,
    *,
    session_factory: Callable[[], httpx.Client] | None = None,
    discover_fn: Callable[[httpx.Client], list[Course]] | None = None,
    resolve_name_fn: Callable[[httpx.Client, str], str] | None = None,
    select_fn: Callable[[list[Course], Settings], list[Course]] | None = None,
    deadlines_fn: Callable[[httpx.Client], dict[str, list[Assignment]]] | None = None,
    materials_fn: Callable[[httpx.Client, str], tuple[list[ContentItem], list[Assignment]]]
    | None = None,
    merge_fn: Callable[[list[Assignment], list[Assignment]], list[Assignment]] | None = None,
    recordings_fn: Callable[[httpx.Client, str], list[Recording]] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    progress_fn: Callable[[int, int, str], None] | None = None,
    max_workers: int = 4,
) -> LiveSnapshot:
    """Authenticate and parse current courses, assignments and recordings.

    The function performs no downloads and writes no files. Calendar failure
    is reported as a snapshot warning, while a material/recording failure is
    isolated to that course so the other rows remain useful. Courses are
    fetched through a small thread pool (``max_workers``, 1 = sequential;
    ``httpx.Client`` is thread-safe) and ``progress_fn(done, total, course)``
    fires after each course completes, so callers can surface progress
    instead of a blank wait. Every dependency is an explicit keyword-only
    callable so callers cannot misspell a name and the orchestration stays
    unit-testable without network access.
    """
    from .auth import get_session
    from .config import settings
    from .discover import (
        discover_courses,
        discover_selected_courses,
        resolve_course_name,
        select_courses,
    )
    from .materials import fetch_deadlines, merge_deadlines, walk_materials
    from .recordings import list_recordings

    config = config or settings
    session_factory = session_factory or get_session
    discover_fn = discover_fn or discover_courses
    resolve_name_fn = resolve_name_fn or resolve_course_name
    select_fn = select_fn or select_courses
    deadlines_fn = deadlines_fn or fetch_deadlines
    materials_fn = materials_fn or walk_materials
    merge_fn = merge_fn or merge_deadlines
    recordings_fn = recordings_fn or list_recordings
    now_fn = now_fn or datetime.now

    client = session_factory()
    try:
        discovered = discover_fn(client)
        if not discovered:
            raise RuntimeError("教学网没有返回任何课程，请检查登录状态或学期筛选")
        selected = discover_selected_courses(
            client,
            config,
            discovered=discovered,
            resolve_name_fn=resolve_name_fn,
            select_fn=select_fn,
        )
        if not selected:
            raise RuntimeError("当前学期或课程筛选没有选中任何教学网课程")

        warnings: list[str] = []
        try:
            by_course = deadlines_fn(client)
        except Exception as exc:  # calendar is useful but not the only assignment source
            by_course = {}
            warnings.append(f"全局截止时间读取失败：{exc}")

        def fetch_one(course: Course) -> tuple[LiveCourseStatus, list[Deadline]]:
            # One course = one unit of work. Calendar failure was already
            # downgraded to plain entries; material/recording failures are
            # isolated here so the other rows remain useful.
            errors: list[str] = []
            calendar_entries = by_course.get(course.course_id, [])
            try:
                _items, tree_assignments = materials_fn(client, course.course_id)
                assignments = merge_fn(tree_assignments, calendar_entries)
            except Exception as exc:
                assignments = list(calendar_entries)
                errors.append(f"作业：{exc}")

            course_deadlines = [
                Deadline(
                    course=course.name,
                    title=assignment.title,
                    due_at=assignment.due_at or "未公布",
                    source=assignment.source,
                    due=parse_due(assignment.due_at),
                )
                for assignment in assignments
            ]

            try:
                recordings = recordings_fn(client, course.course_id)
            except Exception as exc:
                recordings = []
                errors.append(f"录音：{exc}")

            dated = sorted(
                (d.due for d in course_deadlines if d.due is not None),
                key=_datetime_key,
            )
            nearest_due = dated[0].isoformat(sep=" ", timespec="minutes") if dated else ""
            status = LiveCourseStatus(
                course=course.name,
                course_id=course.course_id,
                assignments=len(assignments),
                recordings=len(recordings),
                nearest_due=nearest_due,
                errors=tuple(errors),
            )
            return status, course_deadlines

        statuses: list[LiveCourseStatus] = []
        all_deadlines: list[Deadline] = []
        total = len(selected)

        def collect(result: tuple[LiveCourseStatus, list[Deadline]]) -> None:
            status, course_deadlines = result
            statuses.append(status)
            all_deadlines.extend(course_deadlines)
            if progress_fn is not None:
                progress_fn(len(statuses), total, status.course)

        if max_workers <= 1 or total <= 1:
            for course in selected:
                collect(fetch_one(course))
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [pool.submit(fetch_one, course) for course in selected]
                for future in as_completed(futures):
                    collect(future.result())

        all_deadlines.sort(
            key=lambda item: (
                item.due is None,
                _datetime_key(item.due),
                item.course,
                item.title,
            )
        )
        statuses.sort(key=lambda item: (item.course, item.course_id))
        return LiveSnapshot(
            fetched_at=now_fn(),
            courses=tuple(statuses),
            deadlines=tuple(all_deadlines),
            warnings=tuple(warnings),
        )
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()


def _datetime_key(value: datetime | None) -> tuple[int, int, int, int, int, int]:
    """Sort aware and naive Blackboard timestamps without mixing tzinfo."""
    if value is None:
        return (9999, 12, 31, 23, 59, 59)
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)


def parse_daily_log(text: str) -> DailyRunStatus:
    started_match = _START_RE.search(text)
    exit_match = _EXIT_RE.search(text)
    return DailyRunStatus(
        started=started_match.group(1) if started_match else None,
        exit_code=int(exit_match.group(1)) if exit_match else None,
    )


def parse_due(raw: str) -> datetime | None:
    """Parse the due-date formats Blackboard emits; None when undated.

    Handles ISO-8601 with ``Z`` and .NET's 7-digit fractions, plus the plain
    ``YYYY-MM-DD[ HH:MM[:SS]]`` forms the assignments table prints.
    """
    value = (raw or "").strip()
    if not value or any(value.startswith(prefix) for prefix in _UNDATED):
        return None
    value = value.replace("Z", "+00:00")
    value = _ISO_FRACTION_RE.sub(lambda m: "." + m.group(1)[:6], value)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def collect_deadlines(data_dir: Path) -> list[Deadline]:
    """Merge every course's assignments.md table and deadlines.json.

    The two stores overlap; rows are deduped by (course, title), preferring
    the copy with a parseable date. Undated rows are kept — "no deadline
    published yet" is itself actionable information.
    """
    data_dir = data_dir.expanduser()
    found: dict[tuple[str, str], Deadline] = {}
    if not data_dir.is_dir():
        return []
    for course_dir in sorted(data_dir.iterdir()):
        if not course_dir.is_dir():
            continue
        course = _course_name(course_dir)
        for deadline in _deadlines_from_md(course_dir / "assignments.md", course):
            _keep(found, deadline)
        for deadline in _deadlines_from_json(course_dir / "deadlines.json", course):
            _keep(found, deadline)
    return sorted(
        found.values(),
        key=lambda d: (d.due is None, d.due or datetime.max.replace(tzinfo=None), d.course),
    )


def collect_local_workbench(data_dir: Path) -> LocalWorkbenchSnapshot:
    """Read course materials, recording stages and Notion run reports.

    This is intentionally filesystem-only and bounded to the normalized course
    tree. It lets the TUI show useful content immediately while the live
    Teaching Network request is still authenticating.
    """
    data_dir = data_dir.expanduser()
    courses: list[LocalCourseStatus] = []
    if data_dir.is_dir():
        for course_dir in sorted(data_dir.iterdir()):
            if not course_dir.is_dir():
                continue
            meta_path = course_dir / "course.json"
            try:
                meta = json.loads(meta_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            course_id = str(meta.get("course_id") or "")
            name = str(meta.get("name") or course_dir.name)
            material_files = tuple(
                path.relative_to(course_dir).as_posix()
                for path in sorted((course_dir / "materials").rglob("*"))
                if path.is_file()
            ) if (course_dir / "materials").is_dir() else ()
            announcement_files = tuple(
                path.relative_to(course_dir).as_posix()
                for path in sorted((course_dir / "announcements").glob("*.md"))
                if path.is_file()
            ) if (course_dir / "announcements").is_dir() else ()
            assignment_count = _assignment_count(course_dir / "deadlines.json")
            recordings = _local_recordings(data_dir, course_dir, course_id, name)
            courses.append(
                LocalCourseStatus(
                    folder=course_dir.name,
                    course_id=course_id,
                    name=name,
                    material_files=material_files,
                    announcement_files=announcement_files,
                    assignment_count=assignment_count,
                    recordings=recordings,
                )
            )
    logs = data_dir / "logs"
    return LocalWorkbenchSnapshot(
        courses=tuple(courses),
        notion=NotionRunStatus(
            review_report=latest_report(logs, "review"),
            lecture_report=latest_report(logs, "lecture"),
            review_excerpt=_report_excerpt(logs, "review"),
            lecture_excerpt=_report_excerpt(logs, "lecture"),
        ),
    )


def course_artifacts(data_dir: Path, course: LocalCourseStatus) -> tuple[CourseArtifact, ...]:
    """Build concise assignment, material, announcement and recording entries."""
    root = data_dir.expanduser() / course.folder
    artifacts: list[CourseArtifact] = []
    deadline_path = root / "deadlines.json"
    try:
        deadlines = json.loads(deadline_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        deadlines = []
    if isinstance(deadlines, list):
        for item in deadlines:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            due = str(item.get("due_at") or "未公布")
            instructions = str(item.get("instructions") or "")
            artifacts.append(
                CourseArtifact(
                    "作业",
                    str(item["title"]),
                    _shorten(f"DDL {due}。{instructions}", 120),
                    "deadlines.json",
                )
            )

    for relative in course.material_files:
        path = root / relative
        title = path.name
        summary = _text_summary(path) if path.suffix.lower() in {".md", ".txt"} else "课件文件"
        artifacts.append(CourseArtifact("课件", title, summary, relative))

    for relative in course.announcement_files:
        path = root / relative
        artifacts.append(CourseArtifact("公告", path.stem, _text_summary(path), relative))

    for recording in course.recordings:
        note_path = root / recording.directory / "notes.md"
        summary = (
            f"{recording.status}；笔记 {recording.notes_chars} 字，关键帧 {recording.keyframes} 张。"
        )
        if note_path.exists():
            summary += " " + _text_summary(note_path, 96)
        artifacts.append(
            CourseArtifact(
                "录音",
                recording.title,
                _shorten(summary, 180),
                recording.directory,
                recording.directory,
            )
        )
    return tuple(artifacts)


def _text_summary(path: Path, limit: int = 160) -> str:
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return "文件不可读取"
    lines = [line.strip().lstrip("#").strip() for line in text.splitlines() if line.strip()]
    return _shorten(" ".join(lines[1:] or lines), limit) or "无文本摘要"


def _shorten(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _assignment_count(path: Path) -> int:
    try:
        entries = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return sum(isinstance(entry, dict) and bool(entry.get("title")) for entry in entries) if isinstance(entries, list) else 0


def _local_recordings(
    data_dir: Path,
    course_dir: Path,
    course_id: str,
    course_name: str,
) -> tuple[LocalRecordingStatus, ...]:
    index_path = course_dir / "recordings" / "index.json"
    try:
        entries = json.loads(index_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(entries, list):
        return ()
    from .models import Recording
    from .pipeline import RecordingJob, recording_stage

    rows: list[LocalRecordingStatus] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        try:
            recording = Recording.model_validate(raw)
        except Exception:
            continue
        job = RecordingJob(course_name, course_dir, recording)
        status, unavailable = recording_stage(job)
        directory = job.directory.relative_to(data_dir).as_posix()
        rows.append(
            LocalRecordingStatus(
                title=recording.title,
                recorded_at=recording.recorded_at,
                status=status,
                directory=directory,
                notes_chars=(job.directory / "notes.md").stat().st_size
                if (job.directory / "notes.md").exists()
                else 0,
                keyframes=len(list((job.directory / "keyframes").glob("frame_*.jpg")))
                if (job.directory / "keyframes").is_dir()
                else 0,
                unavailable_reason=unavailable,
            )
        )
    return tuple(rows)


def _report_excerpt(logs: Path, prefix: str, limit: int = 160) -> str:
    name = latest_report(logs, prefix)
    if not name:
        return ""
    try:
        text = (logs / name).read_text("utf-8", errors="replace").strip()
    except OSError:
        return ""
    return " ".join(text.split())[-limit:]


def _course_name(course_dir: Path) -> str:
    meta = course_dir / "course.json"
    try:
        name = json.loads(meta.read_text("utf-8")).get("name") or ""
    except (OSError, json.JSONDecodeError):
        name = ""
    return name or course_dir.name


def _keep(found: dict[tuple[str, str], Deadline], deadline: Deadline) -> None:
    key = (deadline.course, deadline.title)
    existing = found.get(key)
    if existing is None or (deadline.dated and not existing.dated):
        found[key] = deadline


def _deadlines_from_md(path: Path, course: str) -> list[Deadline]:
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return []
    deadlines: list[Deadline] = []
    for line in lines:
        match = _TABLE_ROW_RE.match(line.strip())
        if not match:
            continue
        due_at, title, source = (part.strip() for part in match.groups())
        if due_at.startswith("截止") or set(due_at) <= {"-", " "} or not title:
            continue  # header and separator rows
        deadlines.append(
            Deadline(course=course, title=title, due_at=due_at, source=source, due=parse_due(due_at))
        )
    return deadlines


def _deadlines_from_json(path: Path, course: str) -> list[Deadline]:
    try:
        entries = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(entries, list):
        return []
    deadlines: list[Deadline] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("title"):
            continue
        due_at = str(entry.get("due_at") or "")
        deadlines.append(
            Deadline(
                course=course,
                title=str(entry["title"]),
                due_at=due_at or "未公布",
                source=str(entry.get("source") or ""),
                due=parse_due(due_at),
            )
        )
    return deadlines


def upcoming(
    deadlines: list[Deadline],
    *,
    today: datetime | None = None,
    days: int = 14,
    overdue_days: int = 7,
) -> list[tuple[Deadline, int]]:
    """Dated deadlines inside the window, as (deadline, days_left) pairs.

    ``days_left`` is negative for overdue items, which stay visible for
    ``overdue_days`` so a missed deadline does not silently vanish.
    """
    now = today or datetime.now()
    window: list[tuple[Deadline, int]] = []
    for deadline in deadlines:
        if deadline.due is None:
            continue
        left = (deadline.due.date() - now.date()).days
        if -overdue_days <= left <= days:
            window.append((deadline, left))
    return sorted(window, key=lambda pair: pair[1])


def latest_report(logs_dir: Path, prefix: str) -> str | None:
    """Newest ``<prefix>_YYYYMMDD.md`` report name, or None when absent."""
    if not logs_dir.is_dir():
        return None
    names = sorted(path.name for path in logs_dir.glob(f"{prefix}_*.md"))
    return names[-1] if names else None


def report_date(name: str | None) -> str | None:
    """Extract the YYYY-MM-DD from a report filename, e.g. review_20260916.md."""
    if not name:
        return None
    match = re.search(r"(\d{4})(\d{2})(\d{2})", name)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None


def freshness(date_iso: str | None, today: datetime | None = None) -> str:
    """今天 / 昨天 / N 天前 for a date string; unknown when None."""
    if not date_iso:
        return "无记录"
    try:
        then = datetime.strptime(date_iso, "%Y-%m-%d").date()
    except ValueError:
        return date_iso
    diff = (today or datetime.now()).date() - then
    if diff.days == 0:
        return "今天"
    if diff.days == 1:
        return "昨天"
    if diff.days < 0:
        return "未来"
    return f"{diff.days} 天前"
