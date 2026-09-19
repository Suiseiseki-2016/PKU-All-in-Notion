"""Metadata-only entity models for the Notion directory (exact whitelists).

``model_dump()`` of each model IS the serialization whitelist: nothing else
can cross the adapter boundary. 来源路径 and 备注 are deliberately not
fields (they can contain local-path hints), and no model has any body-text,
media-URL, or local-path field.
"""

from __future__ import annotations

import re
import uuid

from pydantic import BaseModel, Field

from .errors import NotionMetaError

SOURCE_MATERIAL_INDEX = "课程资料索引"
SOURCE_RECORDING_NOTES = "课堂录像笔记"
NOTE_TYPE_LABEL = "课堂笔记"  # synthesized display type for 课堂录像笔记 entries (rule 2)

ASSOCIATION_CONFIRMED = "confirmed"  # explicit stored identity link only
ASSOCIATION_PENDING = "待确认"  # inferred; never shown as confirmed
ASSOCIATION_NONE = "none"  # no lecture link → course-level

_HEX32_RE = re.compile(r"[0-9a-fA-F]{32}")


def canonical_url(page_id: str) -> str:
    """Canonical notion.so URL derived from the id — no workspace search needed."""
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def normalize_page_id(raw: str) -> str:
    """Canonical dashed-UUID form for ids arriving as hex32 (URLs) or dashed."""
    hex32 = _HEX32_RE.search(raw.replace("-", ""))
    if not hex32:
        raise NotionMetaError(f"不是合法的 Notion 页面 id 或 URL: {raw!r}")
    return str(uuid.UUID(hex32.group(0)))


class HubInfo(BaseModel):
    id: str
    url: str
    title: str


class PageRef(BaseModel):
    """A special hub-level page (学习中心 / 课堂录像笔记) or the material database."""

    id: str
    url: str
    title: str


class CourseEntity(BaseModel):
    id: str
    url: str
    title: str  # icon-stripped
    parent: str  # the hub page id


class LectureEntity(BaseModel):
    id: str
    url: str
    title: str
    number: int
    date: str = ""
    period: str = ""
    parent: str  # the course page id


class LectureRef(BaseModel):
    id: str
    url: str


class Association(BaseModel):
    """Lecture linkage; structurally distinct from the 处理状态 select value.

    'confirmed' requires an explicit stored identity link (a 课堂录像笔记 line
    resolving to a lecture page id, or a relation/URL property present in the
    fetched schema). Title-pattern inference is '待确认' and never confirmed.
    """

    state: str = ASSOCIATION_NONE
    lecture: LectureRef | None = None


class MaterialEntity(BaseModel):
    """A 课程资料索引 row — exactly the whitelisted metadata (VAL-META-005)."""

    id: str
    url: str
    title: str
    course: str
    type: str = ""
    status: str = ""
    updated: str = ""
    parent: str  # the 课程资料索引 database id
    source: str = SOURCE_MATERIAL_INDEX
    association: Association = Field(default_factory=Association)


class NoteEntryEntity(BaseModel):
    """A 课堂录像笔记 link line: identity IS the target lecture page.

    Channel (i) explicit association: a line resolving to a lecture page id
    is confirmed-linked to that lecture. A line whose target is not one of
    the directory's lectures keeps its stored identity but stays visibly
    待确认 (never confirmed without directory verification).
    """

    id: str  # target lecture page id (the explicit link channel)
    url: str
    title: str
    course: str
    type: str = NOTE_TYPE_LABEL
    source: str = SOURCE_RECORDING_NOTES
    parent: str  # the 课堂录像笔记 course subpage carrying the line
    association: Association = Field(default_factory=Association)


class ExerciseEntity(BaseModel):
    """A course-page exercise child surfaced as METADATA ONLY (VAL-EXER-037).

    The pinned allowlist is identity + course scope + state signals + updated
    time. The body scan extracts presence booleans only; question text,
    answer text, explanations, and rubrics never cross the adapter boundary.
    """

    id: str
    url: str
    title: str
    course: str
    parent: str  # the course page id
    updated: str = ""
    marker_present: bool = False  # grading-marker heading present (已批改 signal)
    answer_present: bool = False  # a pinned answer area/marker is present


class DirectoryData(BaseModel):
    """One full directory read: metadata-only, every entity with identity."""

    semester: str
    hub: HubInfo
    material_database: PageRef
    # Backend-only grading authority. Exclusion keeps it out of generic
    # metadata serialization and therefore out of every panel payload.
    wrong_answer_database: PageRef | None = Field(default=None, exclude=True)
    wrong_answer_setup_error: str = Field(default="", exclude=True)
    semester_page: PageRef | None = None
    notes_hub: PageRef | None = None
    courses: list[CourseEntity] = Field(default_factory=list)
    lectures: list[LectureEntity] = Field(default_factory=list)
    materials: list[MaterialEntity] = Field(default_factory=list)
    notes: list[NoteEntryEntity] = Field(default_factory=list)
    exercises: list[ExerciseEntity] = Field(default_factory=list)
    materials_by_course: dict[str, list[MaterialEntity]] = Field(default_factory=dict)
    notes_by_course: dict[str, list[NoteEntryEntity]] = Field(default_factory=dict)
    lectures_by_course: dict[str, list[LectureEntity]] = Field(default_factory=dict)
    exercises_by_course: dict[str, list[ExerciseEntity]] = Field(default_factory=dict)
    course_unknown_materials: list[MaterialEntity] = Field(default_factory=list)
    course_unknown_notes: list[NoteEntryEntity] = Field(default_factory=list)
    diagnostics: dict = Field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.model_dump()

    def confirmed_materials_for_lecture(self, lecture_id: str) -> list[MaterialEntity]:
        """Explicitly linked (confirmed) materials of one lecture.

        Inference never enters this query: title-pattern associations are
        待确认 and excluded here (VAL-META-011/012).
        """
        return [
            material
            for material in self.materials
            if material.association.state == ASSOCIATION_CONFIRMED
            and material.association.lecture is not None
            and material.association.lecture.id == lecture_id
        ]

    def inferred_materials_for_lecture(self, lecture_id: str) -> list[MaterialEntity]:
        """Title-inferred (待确认) materials hinting at one lecture — never confirmed."""
        return [
            material
            for material in self.materials
            if material.association.state == ASSOCIATION_PENDING
            and material.association.lecture is not None
            and material.association.lecture.id == lecture_id
        ]
