"""Student panel directory service (VAL-META-015..018).

Builds the M3 directory API payloads on top of the M2 Notion adapter. Every
entity serializes EXACTLY the approved fields — id, url, title, counts, sync
label, scope, mapping state, page state (lectures additionally carry
number/date/period) — and no forbidden string can enter a payload (the
adapter's entity whitelists enforce that upstream; the panel tests scan the
full serialized output for seeded honeypots).

- Directory: hub + courses (+ nested lectures) + honest sync snapshot.
- Material views: 按讲次 (selected lecture only; inferred as a distinctly
  flagged 待确认 subset), 按资料类型 (linked + course-level grouped by type
  with counts), 全部资料 (flat merged list with per-item association labels).
- Launch: identity-only resolution from the LAST LOADED directory — zero
  client calls (no search), missing identity → explicit missing-mapping
  result with a course-level fallback and generic copy.
- Sync surface: the four approved states syncing/error/empty/done plus the
  last-sync timestamp and a user-safe error reason; a failed sync NEVER
  reports done.

The provider protocol keeps the real adapter and the seeded-fake mode
(pku_sync.panel.fake_directory) interchangeable.
"""

from __future__ import annotations

import datetime
import logging
import threading
from pathlib import Path
from typing import Callable, Protocol

from .exercises import ExerciseEventStore, exercise_row

from ..notion_meta import (
    ASSOCIATION_CONFIRMED,
    ASSOCIATION_NONE,
    ASSOCIATION_PENDING,
    LAUNCH_MISSING_MAPPING,
    LAUNCH_OPENED,
    NOTE_TYPE_LABEL,
    DirectoryData,
    LaunchResult,
)

logger = logging.getLogger(__name__)

# -- sync states (the only four the panel may report) --------------------------

STATE_SYNCING = "syncing"
STATE_ERROR = "error"
STATE_EMPTY = "empty"
STATE_DONE = "done"
SYNC_STATES = (STATE_SYNCING, STATE_ERROR, STATE_EMPTY, STATE_DONE)

# User-safe error reasons: never an exception body, token, path, or Notion
# response fragment (honesty + no-credential rules).
SYNC_ERROR_HUB_AMBIGUOUS = "没有找到唯一匹配的学期学习中心，请检查 Notion 空间后重试。"
SYNC_ERROR_HUB_NOT_FOUND = "没有找到当前学期的学习中心，请确认已连接正确的 Notion 空间。"
SYNC_ERROR_SCHEMA = "课程资料索引的属性和预期不一致，请检查数据库后重试。"
SYNC_ERROR_NO_INDEX_DB = "没有找到课程资料索引数据库，请检查 Notion 空间后重试。"
SYNC_ERROR_UNAVAILABLE = "同步服务暂时不可用，请稍后重试。"
SYNC_ERROR_READ = "无法读取 Notion 学习空间，请检查连接后重新同步。"
SYNC_ERROR_GENERIC = "同步遇到问题，请稍后重试。"

# -- launch copy (generic; no token/path/URL fragments) ------------------------

LAUNCH_FAILED = "failed"  # fault-injected launch (recoverable failure state)
LAUNCH_MISSING_MAPPING_COPY = "该条目还没有对应的 Notion 页面，可以先用课程页打开。"
LAUNCH_FAILED_COPY = "打开没有成功，请稍后重试。"

# -- material views -------------------------------------------------------------

MATERIAL_VIEW_LECTURE = "lecture"
MATERIAL_VIEW_TYPE = "type"
MATERIAL_VIEW_ALL = "all"
MATERIAL_VIEWS = (MATERIAL_VIEW_LECTURE, MATERIAL_VIEW_TYPE, MATERIAL_VIEW_ALL)
UNDEFINED_TYPE_LABEL = "未标注"  # empty 资料类型 group label (neutral, never invented)


class DirectoryApiError(RuntimeError):
    """A panel-level API error carrying its own HTTP status + safe message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class DirectoryProvider(Protocol):
    def load(self) -> DirectoryData:
        """Full directory read; explicit exception = the error sync state."""
        ...

    def resolve_launch(
        self, data: DirectoryData, target_id: str, *, course_id: str | None = None
    ) -> LaunchResult:
        """Identity-only launch resolution from the given loaded directory."""
        ...


def _default_now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


class SyncTracker:
    """Honest four-state sync machine with the last-sync timestamp.

    ``begin`` marks syncing (a retry clears the previous error reason), a
    successful load lands in ``empty`` (zero courses) or ``done``, and every
    failure lands in ``error`` carrying a user-safe reason — never ``done``.
    The last-sync timestamp is only ever set by a SUCCESSFUL load and is
    preserved across later failures (the established index is not lost).
    """

    def __init__(self, clock: Callable[[], datetime.datetime] | None = None):
        self._lock = threading.Lock()
        self._clock = clock or _default_now
        self.state = STATE_SYNCING
        self.last_sync_at: str | None = None
        self.error_reason: str | None = None

    def begin(self) -> None:
        with self._lock:
            self.state = STATE_SYNCING
            self.error_reason = None

    def succeed(self, data: DirectoryData) -> None:
        with self._lock:
            self.state = STATE_EMPTY if not data.courses else STATE_DONE
            self.last_sync_at = self._now_iso()
            self.error_reason = None

    def fail(self, reason: str) -> None:
        with self._lock:
            self.state = STATE_ERROR
            self.error_reason = reason

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "last_sync_at": self.last_sync_at,
                "error_reason": self.error_reason,
            }

    def _now_iso(self) -> str:
        return self._clock().isoformat(timespec="seconds")


def user_safe_reason(exc: Exception) -> str:
    """Map adapter/client failures to the pinned user-safe copy."""
    from ..notion import NotionError
    from ..notion_meta.errors import (
        HubAmbiguityError,
        HubNotFoundError,
        MaterialDatabaseNotFoundError,
        NotionMetaError,
        NotionRetryableError,
        SchemaError,
    )

    if isinstance(exc, HubAmbiguityError):
        return SYNC_ERROR_HUB_AMBIGUOUS
    if isinstance(exc, HubNotFoundError):
        return SYNC_ERROR_HUB_NOT_FOUND
    # MaterialDatabaseNotFoundError subclasses SchemaError: check it first so
    # the "missing database" case keeps its own actionable copy
    if isinstance(exc, MaterialDatabaseNotFoundError):
        return SYNC_ERROR_NO_INDEX_DB
    if isinstance(exc, SchemaError):
        return SYNC_ERROR_SCHEMA
    if isinstance(exc, NotionRetryableError):
        return SYNC_ERROR_UNAVAILABLE
    if isinstance(exc, (NotionMetaError, NotionError)):
        return SYNC_ERROR_READ
    return SYNC_ERROR_GENERIC


# -- display helpers -----------------------------------------------------------


def friendly_sync_time(iso: str, now: datetime.datetime | None = None) -> str:
    """'今天 09:12' / '昨天 20:40' / '9月18日 08:30' for the sync label."""
    if not iso:
        return ""
    dt = datetime.datetime.fromisoformat(iso)
    if dt.tzinfo is not None and now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=dt.tzinfo)
    reference = now or _default_now()
    today = reference.date()
    day = dt.date()
    stamp = dt.strftime("%H:%M")
    if day == today:
        return f"今天 {stamp}"
    if day == today - datetime.timedelta(days=1):
        return f"昨天 {stamp}"
    return f"{day.month}月{day.day}日 {stamp}"


def sync_label(last_sync_at: str | None) -> str:
    if not last_sync_at:
        return "已同步"
    return f"已同步 · 最近更新 {friendly_sync_time(last_sync_at)}"


# -- whitelisted payload builders ---------------------------------------------


def material_item(item, *, course_title: str | None = None) -> dict:
    """A material/note row serialized to the exact view allowlist.

    ``linked_to_lecture`` is TRUE only for an explicitly confirmed link; an
    inferred (待确认) association keeps ``association_state`` distinct and is
    never labeled as linked.
    """
    assoc = item.association
    linked = assoc.state == ASSOCIATION_CONFIRMED
    lecture = None
    if assoc.state in (ASSOCIATION_CONFIRMED, ASSOCIATION_PENDING) and assoc.lecture:
        lecture = {"id": assoc.lecture.id, "url": assoc.lecture.url}
    return {
        "id": item.id,
        "url": item.url,
        "title": item.title,
        "type": item.type,
        "status": getattr(item, "status", ""),  # note entries carry no 处理状态
        "source": item.source,
        "course": course_title if course_title is not None else item.course,
        "is_note": item.type == NOTE_TYPE_LABEL,
        "linked_to_lecture": linked,
        "association_state": assoc.state,
        "lecture": lecture,
        "updated": getattr(item, "updated", ""),  # note entries carry no timestamp
    }


def _course_pool(data: DirectoryData, course) -> list:
    """The course's full index pool: 课程资料索引 rows + 课堂录像笔记 entries."""
    return list(data.materials_by_course.get(course.id, [])) + list(
        data.notes_by_course.get(course.id, [])
    )


def _ordered_pool(data: DirectoryData, course) -> list:
    """linked (.concat) first, then course-level; each bucket sorted."""
    confirmed: list = []
    rest: list = []
    for item in _course_pool(data, course):
        (confirmed if item.association.state == ASSOCIATION_CONFIRMED else rest).append(item)
    confirmed.sort(key=lambda item: (item.source, item.title, item.id))
    rest.sort(key=lambda item: (item.source, item.title, item.id))
    return confirmed + rest


def _lecture_group(data: DirectoryData, lecture) -> tuple[list, list]:
    """(confirmed, inferred) items of one lecture — confirmed materials and
    confirmed note entries first, then the title-inferred 待确认 subset."""
    confirmed = list(data.confirmed_materials_for_lecture(lecture.id))
    inferred = list(data.inferred_materials_for_lecture(lecture.id))
    for note in data.notes:
        if (
            note.association.state == ASSOCIATION_CONFIRMED
            and note.association.lecture is not None
            and note.association.lecture.id == lecture.id
        ):
            confirmed.append(note)
    confirmed.sort(key=lambda item: (item.source, item.title, item.id))
    inferred.sort(key=lambda item: (item.source, item.title, item.id))
    return confirmed, inferred


def _lecture_mapping(confirmed_count: int) -> str:
    return "mapped" if confirmed_count > 0 else "unmapped"


def build_hub_payload(data: DirectoryData, sync: dict) -> dict:
    mapped_courses = any(
        any(_lecture_group(data, lecture)[0] for lecture in data.lectures_by_course.get(course.id, []))
        for course in data.courses
    )
    total_materials = sum(
        len(_course_pool(data, course)) for course in data.courses
    )
    return {
        "id": data.hub.id,
        "url": data.hub.url,
        "title": data.hub.title,
        "counts": {
            "courses": len(data.courses),
            "lectures": len(data.lectures),
            "materials": total_materials,
        },
        "sync_label": sync_label(sync.get("last_sync_at")),
        "scope": data.semester,
        "mapping_state": "mapped" if mapped_courses else "unmapped",
        "page_state": "ready",
    }


def build_lecture_payload(lecture, course, data: DirectoryData, sync: dict) -> dict:
    confirmed, inferred = _lecture_group(data, lecture)
    state = _lecture_mapping(len(confirmed))
    return {
        "id": lecture.id,
        "url": lecture.url,
        "title": lecture.title,
        "number": lecture.number,
        "date": lecture.date,
        "period": lecture.period,
        "counts": {
            "confirmed_materials": len(confirmed),
            "inferred_materials": len(inferred),
            "related_materials": len(confirmed),
        },
        "sync_label": (
            "已同步 · 资料映射待确认" if state == "unmapped"
            else sync_label(sync.get("last_sync_at"))
        ),
        "scope": course.title,
        "mapping_state": state,
        "page_state": "ready",
    }


def build_course_payload(course, data: DirectoryData, sync: dict) -> dict:
    lectures = data.lectures_by_course.get(course.id, [])
    mapped = any(
        _lecture_group(data, lecture)[0] for lecture in lectures
    )
    notes = data.notes_by_course.get(course.id, [])
    indexed = [
        m for m in data.materials_by_course.get(course.id, []) if m.status == "已索引"
    ]
    return {
        "id": course.id,
        "url": course.url,
        "title": course.title,
        "counts": {
            "lectures": len(lectures),
            "materials": len(_course_pool(data, course)),
            "notes": len(notes),
            "indexed_materials": len(indexed),
        },
        "sync_label": sync_label(sync.get("last_sync_at")),
        "scope": data.semester,
        "mapping_state": "mapped" if mapped else "unmapped",
        "page_state": "ready",
        "lectures": [
            build_lecture_payload(lecture, course, data, sync) for lecture in lectures
        ],
    }


ACTIVITY_LIMIT = 3  # the dashboard's 最近动态 feed length


def build_activity(data: DirectoryData) -> list[dict]:
    """The newest indexed rows, as the dashboard's 最近动态 entries.

    Derived entirely from the adapter's read: a row only appears once it
    carries a real last-edited timestamp, and the serialized keys stay inside
    the material allowlist (identity + course scope + type/status + updated).
    """
    rows: list[tuple] = []
    for course in data.courses:
        for item in data.materials_by_course.get(course.id, []):
            if item.updated:
                rows.append((item.updated, item.title, item, course))
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [
        {
            "id": item.id,
            "url": item.url,
            "title": item.title,
            "course": course.title,
            "type": item.type or UNDEFINED_TYPE_LABEL,
            "status": item.status,
            "updated": item.updated,
        }
        for _, _, item, course in rows[:ACTIVITY_LIMIT]
    ]


def build_directory_payload(
    data: DirectoryData, sync: dict, *, launched_exercise_ids=()
) -> dict:
    indexed = sum(
        1
        for course in data.courses
        for m in data.materials_by_course.get(course.id, [])
        if m.status == "已索引"
    )
    stats = {
        "courses": len(data.courses),
        "lectures": len(data.lectures),
        "materials": sum(len(_course_pool(data, course)) for course in data.courses),
        "indexed_materials": indexed,
    }
    launched = set(launched_exercise_ids)
    return {
        "semester": data.semester,
        "sync": dict(sync),
        "hub": build_hub_payload(data, sync),
        "stats": stats,
        "activity": build_activity(data),
        "courses": [build_course_payload(course, data, sync) for course in data.courses],
        "exercises": [
            exercise_row(item, launch_started=item.id in launched)
            for item in data.exercises
        ],
    }


# -- material view payloads -----------------------------------------------------


def build_lecture_view(data: DirectoryData, course, lecture) -> dict:
    confirmed, inferred = _lecture_group(data, lecture)
    return {
        "view": MATERIAL_VIEW_LECTURE,
        "course_id": course.id,
        "lecture_id": lecture.id,
        "lecture": {"id": lecture.id, "url": lecture.url, "title": lecture.title},
        "mapped": bool(confirmed),
        "confirmed": [material_item(item, course_title=course.title) for item in confirmed],
        "inferred": [material_item(item, course_title=course.title) for item in inferred],
    }


def build_type_view(data: DirectoryData, course) -> dict:
    groups: list[dict] = []
    by_label: dict[str, list] = {}
    for item in _ordered_pool(data, course):
        label = item.type or UNDEFINED_TYPE_LABEL
        bucket = by_label.get(label)
        if bucket is None:
            bucket = []
            by_label[label] = bucket
            groups.append({"type": label, "count": 0, "items": bucket})
        bucket.append(material_item(item, course_title=course.title))
    for group in groups:
        group["count"] = len(group["items"])
    return {"view": MATERIAL_VIEW_TYPE, "course_id": course.id, "groups": groups}


def build_all_view(data: DirectoryData, course) -> dict:
    return {
        "view": MATERIAL_VIEW_ALL,
        "course_id": course.id,
        "items": [
            material_item(item, course_title=course.title)
            for item in _ordered_pool(data, course)
        ],
    }


def _find_course(data: DirectoryData, course_id: str) -> object:
    for course in data.courses:
        if course.id == course_id:
            return course
    raise DirectoryApiError(404, "找不到该课程，请刷新目录后重试。")


def _find_lecture(data: DirectoryData, course, lecture_id: str | None):
    if not lecture_id:
        raise DirectoryApiError(400, "按讲次视图需要先选择一个讲次。")
    for lecture in data.lectures_by_course.get(course.id, []):
        if lecture.id == lecture_id:
            return lecture
    raise DirectoryApiError(404, "找不到该讲次，请刷新目录后重试。")


# -- service -------------------------------------------------------------------


class DirectoryService:
    """Holds the provider + the honest sync tracker.

    ``load`` is the only operation that touches the Notion side; material
    views and launch resolution operate on the last successfully loaded
    directory, so launch paths make zero client calls (no search anywhere).
    """

    def __init__(
        self, provider: DirectoryProvider, *, clock=None, exercise_events=None
    ):
        self.provider = provider
        self.exercise_events = exercise_events or ExerciseEventStore()
        self.sync = SyncTracker(clock=clock)
        self._data: DirectoryData | None = None
        self._organized_exercises: dict[str, object] = {}
        self._lock = threading.Lock()

    def sync_snapshot(self) -> dict:
        return self.sync.snapshot()

    def load(self) -> dict:
        """One full directory read with honest sync-state transitions.

        Any failure lands in the error state with a user-safe reason and
        raises :class:`DirectoryApiError` (503) so the API never serves an
        empty directory as success.
        """
        self.sync.begin()
        try:
            data = self.provider.load()
        except Exception as exc:  # noqa: BLE001 - every read failure is an explicit state
            try:
                reason = user_safe_reason(exc)
            except Exception:  # never let reason mapping itself leak anything
                reason = SYNC_ERROR_GENERIC
            self.sync.fail(reason)
            raise DirectoryApiError(503, reason) from exc
        with self._lock:
            for entity in self._organized_exercises.values():
                if all(item.id != entity.id for item in data.exercises):
                    data.exercises.append(entity)
                    data.exercises_by_course.setdefault(entity.parent, []).append(entity)
            self._data = data
        self.sync.succeed(data)
        return build_directory_payload(
            data,
            self.sync.snapshot(),
            launched_exercise_ids=self.exercise_events.launched_ids(),
        )

    def mark_exercise_launched(self, exercise_id: str) -> None:
        """Record a Notion answering launch without storing exercise content."""
        self.exercise_events.mark_launched(exercise_id)

    def register_exercise(self, entity) -> None:
        """Add a newly organized page to subsequent metadata-only payloads."""
        with self._lock:
            self._organized_exercises[entity.id] = entity
            if self._data is not None and all(item.id != entity.id for item in self._data.exercises):
                self._data.exercises.append(entity)
                self._data.exercises_by_course.setdefault(entity.parent, []).append(entity)
    def invalidate(self) -> None:
        """Drop the cached directory (used when the Notion token is cleared).

        A disconnected panel must not serve the index it read while
        connected, and the next read must go back to the workspace.
        """
        with self._lock:
            self._data = None

    def ensure_loaded(self) -> DirectoryData:
        with self._lock:
            data = self._data
        if data is not None:
            return data
        self.load()  # first read uses the same sync/error semantics
        with self._lock:
            return self._data

    def material_view(
        self, course_id: str, view: str, *, lecture_id: str | None = None
    ) -> dict:
        if view not in MATERIAL_VIEWS:
            raise DirectoryApiError(400, f"未知的资料视图：{view}。")
        data = self.ensure_loaded()
        course = _find_course(data, course_id)
        if view == MATERIAL_VIEW_LECTURE:
            lecture = _find_lecture(data, course, lecture_id)
            return build_lecture_view(data, course, lecture)
        if view == MATERIAL_VIEW_TYPE:
            return build_type_view(data, course)
        return build_all_view(data, course)

    def resolve_launch(
        self, target_id: str, *, course_id: str | None = None
    ) -> LaunchResult:
        with self._lock:
            data = self._data
        if data is None:
            # no loaded directory: nothing to resolve against — an explicit
            # missing-mapping result, never a guess and never a search
            return LaunchResult(
                status=LAUNCH_MISSING_MAPPING,
                target_id=(target_id or "").strip(),
                url=None,
                fallback=None,
            )
        return self.provider.resolve_launch(data, target_id, course_id=course_id)


# -- real provider + factory ----------------------------------------------------


class RealDirectoryProvider:
    """The production provider: a lazily-built real Notion adapter.

    Nothing is constructed until the first read, so creating the app never
    requires (or fails on) a missing NOTION_TOKEN.
    """

    def __init__(self, settings, *, semester: str | None = None, cache_path=None):
        self._settings = settings
        self._semester = semester
        self._cache_path = cache_path
        self._directory = None

    def _ensure(self):
        if self._directory is None:
            from ..notion import get_client
            from ..notion_meta import NotionDirectory

            client = get_client(self._settings)
            cache_path = self._cache_path
            if cache_path is None:
                cache_path = Path(self._settings.data_dir) / "notion" / "identity_cache.json"
            self._directory = NotionDirectory(
                client, semester=self._semester, cache_path=cache_path
            )
        return self._directory

    def load(self) -> DirectoryData:
        return self._ensure().load()

    def resolve_launch(
        self, data: DirectoryData, target_id: str, *, course_id: str | None = None
    ) -> LaunchResult:
        from ..notion_meta import LaunchResolver

        return LaunchResolver(data).resolve(target_id, course_id=course_id)


def make_directory_service(settings=None, *, semester: str | None = None, clock=None) -> DirectoryService:
    """The production panel service (real adapter, lazily accessed)."""
    if settings is None:
        from ..config import settings as default_settings

        settings = default_settings
    event_path = Path(settings.data_dir) / "panel" / "exercise_events.json"
    return DirectoryService(
        RealDirectoryProvider(settings, semester=semester),
        clock=clock,
        exercise_events=ExerciseEventStore(event_path),
    )


__all__ = [
    "STATE_SYNCING",
    "STATE_ERROR",
    "STATE_EMPTY",
    "STATE_DONE",
    "SYNC_STATES",
    "LAUNCH_FAILED",
    "LAUNCH_MISSING_MAPPING",
    "LAUNCH_OPENED",
    "LAUNCH_MISSING_MAPPING_COPY",
    "LAUNCH_FAILED_COPY",
    "MATERIAL_VIEW_LECTURE",
    "MATERIAL_VIEW_TYPE",
    "MATERIAL_VIEW_ALL",
    "MATERIAL_VIEWS",
    "UNDEFINED_TYPE_LABEL",
    "ACTIVITY_LIMIT",
    "build_activity",
    "ASSOCIATION_NONE",
    "ASSOCIATION_PENDING",
    "ASSOCIATION_CONFIRMED",
    "DirectoryApiError",
    "DirectoryProvider",
    "SyncTracker",
    "user_safe_reason",
    "friendly_sync_time",
    "sync_label",
    "material_item",
    "build_directory_payload",
    "build_lecture_view",
    "build_type_view",
    "build_all_view",
    "build_lecture_payload",
    "build_course_payload",
    "build_hub_payload",
    "_course_pool",
    "_ordered_pool",
    "_lecture_group",
    "DirectoryService",
    "RealDirectoryProvider",
    "make_directory_service",
]
