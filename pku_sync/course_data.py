"""Read-only course metadata and assignment date parsing."""


from __future__ import annotations




import re


from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait


from dataclasses import dataclass


from datetime import datetime




from threading import Event


from typing import TYPE_CHECKING, Any, Callable, Iterable


if TYPE_CHECKING:
    import httpx

    from .config import Settings
    from .models import Assignment, ContentItem, Course, Recording


_ISO_FRACTION_RE = re.compile(r"\.(\d{7,})")


_UNDATED = ("未公布", "待确认", "无", "-")


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


@dataclass(frozen=True)
class CourseMetadataRead:
    course_id: str
    recordings: list[Any] | None = None
    material_items: list[Any] | None = None
    tree_assignments: list[Any] | None = None
    notices: list[Any] | None = None
    recordings_error: Exception | None = None
    materials_error: Exception | None = None
    notices_error: Exception | None = None
    recordings_status: str = "not_read"
    materials_status: str = "not_read"
    notice_status: str = "not_read"


class _CollectionStopped(Exception):
    pass


def _check_rows(rows: Any, course_id: str) -> None:
    if not isinstance(rows, list):
        raise ValueError("course metadata did not return a complete list")
    if any(getattr(row, "course_id", None) != course_id for row in rows):
        raise ValueError("course metadata returned rows for a different course")


def read_course_metadata(
    client: Any,
    course_ids: Iterable[str],
    *,
    recordings_fn: Callable,
    materials_fn: Callable,
    announcements_fn: Callable,
    check_cancel: Callable[[], None] | None = None,
    max_workers: int = 4,
) -> dict[str, CourseMetadataRead]:
    """Read each course's existing parsers in order, up to four courses at once.

    Cancellation checks run only on the calling thread. If cancellation raises,
    pending work is canceled, in-flight reads drain, and no partial result is
    returned to a publisher. The client remains open for its owner to close.
    """
    from .materials import ToolUnavailable

    ids = tuple(course_ids)
    if any(not isinstance(course_id, str) or not course_id for course_id in ids):
        raise ValueError("invalid course identity")
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate course identity")
    if not isinstance(max_workers, int) or max_workers < 1:
        raise ValueError("max_workers must be positive")
    workers = min(max_workers, 4, len(ids))
    stopped = Event()

    def check_owner() -> None:
        if check_cancel is not None:
            check_cancel()

    def fetch_one(course_id: str, *, serial: bool = False) -> CourseMetadataRead:
        def checkpoint() -> None:
            if serial:
                check_owner()
            if stopped.is_set():
                raise _CollectionStopped()

        checkpoint()
        try:
            recordings = recordings_fn(client, course_id)
            _check_rows(recordings, course_id)
        except Exception as exc:
            return CourseMetadataRead(course_id, recordings_error=exc,
                                      recordings_status="error")

        checkpoint()
        try:
            material_items, tree_assignments = materials_fn(client, course_id)
            _check_rows(material_items, course_id)
            _check_rows(tree_assignments, course_id)
            materials_error = None
        except Exception as exc:
            material_items = tree_assignments = None
            materials_error = exc

        checkpoint()
        try:
            notices = announcements_fn(client, course_id)
            _check_rows(notices, course_id)
            notices_error, notice_status = None, "available"
        except ToolUnavailable as exc:
            notices, notices_error, notice_status = None, exc, "unavailable"
        except Exception as exc:
            notices, notices_error, notice_status = None, exc, "error"
        checkpoint()
        return CourseMetadataRead(
            course_id=course_id, recordings=recordings,
            material_items=material_items, tree_assignments=tree_assignments,
            notices=notices, recordings_status="available",
            materials_status="error" if materials_error is not None else "available",
            notice_status=notice_status, materials_error=materials_error,
            notices_error=notices_error,
        )

    check_owner()
    if workers <= 1:
        results = {}
        for course_id in ids:
            results[course_id] = fetch_one(course_id, serial=True)
        check_owner()
        return results

    # Submit only one bounded window. Cancellation never leaves a backlog of
    # untouched courses that can later issue requests after the owner exits.
    iterator = iter(ids)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="campus-metadata")
    pending = {}
    results = {}
    try:
        for _ in range(workers):
            check_owner()
            course_id = next(iterator)
            pending[pool.submit(fetch_one, course_id)] = course_id
        while pending:
            check_owner()
            completed, _ = wait(pending, timeout=.02, return_when=FIRST_COMPLETED)
            check_owner()
            for future in completed:
                course_id = pending.pop(future)
                results[course_id] = future.result()
            for _ in completed:
                check_owner()
                course_id = next(iterator, None)
                if course_id is not None:
                    pending[pool.submit(fetch_one, course_id)] = course_id
        check_owner()
        return {course_id: results[course_id] for course_id in ids}
    except BaseException:
        stopped.set()
        for future in pending:
            future.cancel()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
