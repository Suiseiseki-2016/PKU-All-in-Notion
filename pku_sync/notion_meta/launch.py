"""Identity-only launch resolution (VAL-META-014).

Every launch path (lecture, material, notes, exercise, course fallback)
resolves from STORED identity — the directory's retained page ids and
canonical URLs — never a workspace search, never a most-recent-page fallback.
A missing identity yields an explicit missing-mapping result carrying the
course-level fallback identity; no URL is ever fabricated.
"""

from __future__ import annotations

from pydantic import BaseModel

from .entities import DirectoryData, PageRef, normalize_page_id
from .errors import NotionMetaError

LAUNCH_OPENED = "opened"
LAUNCH_MISSING_MAPPING = "missing_mapping"


class LaunchResult(BaseModel):
    """Serialization allowlist: exactly status/target_id/url/fallback.

    No token, no local path, no body content — the URL is the stored
    canonical page identity or nothing at all.
    """

    status: str  # LAUNCH_OPENED | LAUNCH_MISSING_MAPPING
    target_id: str
    url: str | None = None
    fallback: PageRef | None = None  # course-level fallback identity


class LaunchResolver:
    """Resolves launch targets from a loaded directory's stored identity.

    Holds NO client reference: resolution is a pure lookup, so no launch
    path can perform a workspace search (asserted by mock call logs).
    """

    def __init__(self, data: DirectoryData):
        entities: list = [
            data.hub,
            data.semester_page,
            data.notes_hub,
            data.material_database,
            *data.courses,
            *data.lectures,
            *data.materials,
            *data.notes,
            *data.exercises,
        ]
        self._urls: dict[str, str] = {}
        for entity in entities:
            if entity is not None:
                self._urls[entity.id] = entity.url
        self._courses = {course.id: course for course in data.courses}

    def resolve(self, target_id: str, *, course_id: str | None = None) -> LaunchResult:
        """Known identity → opened with the exact stored URL; missing → an
        explicit missing-mapping result with the course-level fallback."""
        normalized = self._normalize(target_id)
        url = self._urls.get(normalized)
        if url is not None:
            return LaunchResult(status=LAUNCH_OPENED, target_id=normalized, url=url)
        course = self._courses.get(course_id) if course_id else None
        fallback = (
            PageRef(id=course.id, url=course.url, title=course.title) if course else None
        )
        return LaunchResult(
            status=LAUNCH_MISSING_MAPPING,
            target_id=normalized,
            url=None,
            fallback=fallback,
        )

    @staticmethod
    def _normalize(target_id: str) -> str:
        raw = (target_id or "").strip()
        try:
            return normalize_page_id(raw)
        except NotionMetaError:
            return raw  # unparseable targets are simply missing identities
