"""Association resolution (VAL-META-011/012): explicit stored identity links ONLY.

Explicit iff (ii) a 课程资料索引 relation/URL property — consumed only when
present in the fetched schema — or a canonical lecture URL in 备注. Channel
(i), a 课堂录像笔记 line resolving to a lecture page id, is handled where the
notes are read. Title-pattern inference ("第一讲（1）课程介绍.pdf" → 第一讲)
yields 待确认, never confirmed; an explicit link overrides conflicting
inference; neither → course-level (no association).

备注 values are READ here to consume an identity link but never serialized:
the entity models have no 备注/来源路径 fields, so nothing else can leak
(library/notion-workspace.md rule 1).
"""

from __future__ import annotations

import re

from .entities import (
    ASSOCIATION_CONFIRMED,
    ASSOCIATION_NONE,
    ASSOCIATION_PENDING,
    Association,
    CourseEntity,
    LectureEntity,
    LectureRef,
    MaterialEntity,
    normalize_page_id,
)
from .errors import NotionMetaError
from .titles import parse_lecture_number

_INFERRED_LECTURE_RE = re.compile(r"第\s*(?P<num>[0-9]+|[零一二三四五六七八九十百]+)\s*讲")
_NOTION_URL_RE = re.compile(r"https?://\S*notion\.so/\S+")
_HEX32_RE = re.compile(r"([0-9a-fA-F]{32})")


def resolve_material_associations(
    *,
    schema: dict,
    rows: list[dict],
    materials: list[MaterialEntity],
    lectures: list[LectureEntity],
    lectures_by_course: dict[str, list[LectureEntity]],
    courses: list[CourseEntity],
) -> None:
    """Set each material's association in place (rows ∥ materials, by order).

    Explicit link first (it overrides any conflicting title inference), then
    title-pattern inference → 待确认, else course-level (none).
    """
    lecture_by_id = {lecture.id: lecture for lecture in lectures}
    course_by_title = {course.title: course for course in courses}
    for row, material in zip(rows, materials):
        explicit = _explicit_lecture_ref(schema, row, lecture_by_id)
        if explicit is not None:
            material.association = Association(
                state=ASSOCIATION_CONFIRMED, lecture=explicit
            )
            continue
        inferred = _inferred_lecture_ref(material, course_by_title, lectures_by_course)
        if inferred is not None:
            material.association = Association(state=ASSOCIATION_PENDING, lecture=inferred)
            continue
        material.association = Association(state=ASSOCIATION_NONE, lecture=None)


# -- channel (ii): explicit identity links in the fetched schema/row ------------


def _explicit_lecture_ref(schema, row, lecture_by_id) -> LectureRef | None:
    """A schema-declared relation/URL property, or a canonical lecture URL in
    备注. Only values resolving to a KNOWN lecture page count as explicit."""
    props = row.get("properties") or {}
    schema_props = (schema or {}).get("properties") or {}
    for name, prop in props.items():
        declared = (schema_props.get(name) or {}).get("type")
        if declared == "relation":
            ref = _lecture_ref_from_relation(prop, lecture_by_id)
            if ref is not None:
                return ref
        elif declared == "url":
            ref = _lecture_ref_from_url_token(prop.get("url") or "", lecture_by_id)
            if ref is not None:
                return ref
    if (schema_props.get("备注") or {}).get("type") == "rich_text":
        for token in _NOTION_URL_RE.findall(_plain_text(props.get("备注"))):
            ref = _lecture_ref_from_url_token(token, lecture_by_id)
            if ref is not None:
                return ref
    return None


def _lecture_ref_from_relation(prop, lecture_by_id) -> LectureRef | None:
    for relation in (prop or {}).get("relation") or []:
        lecture = _known_lecture(relation.get("id") or "", lecture_by_id)
        if lecture is not None:
            return LectureRef(id=lecture.id, url=lecture.url)
    return None


def _lecture_ref_from_url_token(token: str, lecture_by_id) -> LectureRef | None:
    for hex_id in _HEX32_RE.findall(token):
        lecture = _known_lecture(hex_id, lecture_by_id)
        if lecture is not None:
            return LectureRef(id=lecture.id, url=lecture.url)
    return None


def _known_lecture(page_id: str, lecture_by_id) -> LectureEntity | None:
    if not page_id:
        return None
    try:
        normalized = normalize_page_id(page_id)
    except NotionMetaError:
        return None  # not a page identity → not an explicit lecture link
    return lecture_by_id.get(normalized)


# -- title-pattern inference (never confirmed) ------------------------------------


def _inferred_lecture_ref(
    material: MaterialEntity,
    course_by_title: dict[str, CourseEntity],
    lectures_by_course: dict[str, list[LectureEntity]],
) -> LectureRef | None:
    """The first 第N讲 token in the title hints at lecture N of the row's
    known course. Requires a known course and a lecture of that number;
    the association stays 待确认 (never counted as confirmed)."""
    match = _INFERRED_LECTURE_RE.search(material.title)
    if match is None:
        return None
    number = parse_lecture_number(match.group("num"))
    if number is None:
        return None
    course = course_by_title.get(material.course)
    if course is None:
        return None  # course-unknown rows have no lecture set to point at
    candidates = [
        lecture
        for lecture in lectures_by_course.get(course.id, [])
        if lecture.number == number
    ]
    if not candidates:
        return None
    # two lectures can share a number (verified fixture: two 第二讲); the
    # first in directory order is the hint target — the 待确认 state keeps
    # the assignment visibly uncertain either way.
    hint = candidates[0]
    return LectureRef(id=hint.id, url=hint.url)


def _plain_text(prop: dict | None) -> str:
    if not prop:
        return ""
    pieces = prop.get("rich_text") or []
    return "".join(piece.get("plain_text", "") for piece in pieces)
