"""The Notion metadata directory adapter (M2, VAL-META-001..009).

Notion-first and metadata-only: hub → courses → lectures, the 课程资料索引
rows, and the 课堂录像笔记 link lines. Recognition is by parent chain plus
known specials (pages carry only a title property). Nothing is synthesized
from the local course tree; the adapter never reads page bodies except the
课堂录像笔记 subpage line scan, and never reads the 学习中心 body at all.
"""

from __future__ import annotations

from . import materials as materials_mod
from .cache import IdentityCache
from .discovery import discover_hub
from .entities import (
    NOTE_TYPE_LABEL,
    SOURCE_RECORDING_NOTES,
    DirectoryData,
    CourseEntity,
    LectureEntity,
    NoteEntryEntity,
    PageRef,
    canonical_url,
)
from .errors import MaterialDatabaseNotFoundError
from .notes import parse_note_lines
from .titles import (
    current_semester_label,
    is_learning_center,
    is_noise_child,
    is_recording_notes_hub,
    normalize_title,
    parse_lecture_title,
)


class NotionDirectory:
    """Builds the metadata directory from a Notion client (real or fake).

    ``client`` is any object with the ``NotionClient`` surface used here:
    ``search`` / ``list_children`` / ``get_database`` / ``query_database``.
    """

    def __init__(self, client, *, semester: str | None = None, now=None, cache_path=None):
        self._client = client
        self._semester = semester
        self._now = now
        self._cache = IdentityCache(cache_path) if cache_path is not None else None

    def load(self) -> DirectoryData:
        """One full directory read; every discovery failure is an explicit error."""
        label = self._semester or current_semester_label(self._now)
        hub = discover_hub(self._client, semester=label)
        blocks = self._client.list_children(hub.id)
        courses, semester_pages, notes_hubs, noise_titles, databases = self._classify(blocks, hub.id)

        index_block = next(
            (db for db in databases if db["title"] == materials_mod.MATERIAL_DATABASE_TITLE),
            None,
        )
        if index_block is None:
            raise MaterialDatabaseNotFoundError([db["title"] for db in databases])
        material_database = PageRef(
            id=index_block["id"], url=canonical_url(index_block["id"]), title=index_block["title"]
        )

        # Schema first, then rows (VAL-META-004): an explicit error on any
        # missing/renamed/wrong-type property, never a silent empty directory.
        schema = self._client.get_database(index_block["id"])
        materials_mod.validate_material_schema(schema)
        rows = self._client.query_database(index_block["id"])
        material_entities = [
            materials_mod.serialize_material_row(row, database_id=index_block["id"])
            for row in rows
        ]

        lectures, lectures_by_course = self._read_lectures(courses)
        notes, notes_by_course, course_unknown_notes, skipped_lines = self._read_notes(
            notes_hubs, courses
        )
        materials_by_course, course_unknown_materials = self._match_courses(
            courses, material_entities
        )

        data = DirectoryData(
            semester=label,
            hub=hub,
            material_database=material_database,
            semester_page=semester_pages[0] if semester_pages else None,
            notes_hub=notes_hubs[0] if notes_hubs else None,
            courses=courses,
            lectures=lectures,
            materials=material_entities,
            notes=notes,
            materials_by_course=materials_by_course,
            notes_by_course=notes_by_course,
            lectures_by_course=lectures_by_course,
            course_unknown_materials=course_unknown_materials,
            course_unknown_notes=course_unknown_notes,
            diagnostics={
                "excluded_hub_children": noise_titles,
                "course_unknown_materials": len(course_unknown_materials),
                "course_unknown_notes": len(course_unknown_notes),
                "skipped_note_lines": skipped_lines,
            },
        )
        if self._cache is not None:
            self._cache.save(data)
        return data

    # -- hub children ---------------------------------------------------------

    def _classify(self, blocks: list[dict], hub_id: str):
        """Direct hub children → courses + specials + noise + databases."""
        courses: list[CourseEntity] = []
        semester_pages: list[PageRef] = []
        notes_hubs: list[PageRef] = []
        noise_titles: list[str] = []
        databases: list[dict] = []
        for block in blocks:
            kind = block.get("type")
            if kind == "child_database":
                title = normalize_title((block.get("child_database") or {}).get("title", ""))
                databases.append({"id": block["id"], "title": title})
            elif kind == "child_page":
                raw_title = (block.get("child_page") or {}).get("title", "")
                title = normalize_title(raw_title)
                if is_learning_center(title):
                    semester_pages.append(
                        PageRef(id=block["id"], url=canonical_url(block["id"]), title=title)
                    )
                elif is_recording_notes_hub(title):
                    notes_hubs.append(
                        PageRef(id=block["id"], url=canonical_url(block["id"]), title=title)
                    )
                elif is_noise_child(title):
                    noise_titles.append(title)
                else:
                    courses.append(
                        CourseEntity(
                            id=block["id"],
                            url=canonical_url(block["id"]),
                            title=title,
                            parent=hub_id,
                        )
                    )
            # any other block type is hub body content — ignored, never captured
        return courses, semester_pages, notes_hubs, noise_titles, databases

    # -- lectures ---------------------------------------------------------------

    def _read_lectures(self, courses: list[CourseEntity]):
        """Course-page children matching ^《?第…讲 are lectures; others are not."""
        lectures: list[LectureEntity] = []
        by_course: dict[str, list[LectureEntity]] = {}
        for course in courses:
            course_lectures: list[LectureEntity] = []
            for block in self._client.list_children(course.id):
                if block.get("type") != "child_page":
                    continue  # body content is ignored, never captured
                raw_title = (block.get("child_page") or {}).get("title", "")
                parsed = parse_lecture_title(raw_title)
                if parsed is None:
                    continue  # 《课程总结》 and similar stay untyped
                course_lectures.append(
                    LectureEntity(
                        id=block["id"],
                        url=canonical_url(block["id"]),
                        title=normalize_title(raw_title),
                        number=parsed.number,
                        date=parsed.date,
                        period=parsed.period,
                        parent=course.id,
                    )
                )
            by_course[course.id] = course_lectures
            lectures.extend(course_lectures)
        return lectures, by_course

    # -- 课堂录像笔记 note entries ----------------------------------------------

    def _read_notes(self, notes_hubs: list[PageRef], courses: list[CourseEntity]):
        """Course-subpage link lines → note entries; zero subpages is normal."""
        course_by_title = {course.title: course for course in courses}
        entries: list[NoteEntryEntity] = []
        by_course: dict[str, list[NoteEntryEntity]] = {course.id: [] for course in courses}
        course_unknown: list[NoteEntryEntity] = []
        skipped_lines = 0
        if not notes_hubs:
            return entries, by_course, course_unknown, skipped_lines
        for sub_block in self._client.list_children(notes_hubs[0].id):
            if sub_block.get("type") != "child_page":
                continue
            subpage_id = sub_block["id"]
            sub_title = normalize_title((sub_block.get("child_page") or {}).get("title", ""))
            lines, skipped = parse_note_lines(self._client.list_children(subpage_id))
            skipped_lines += skipped
            course = course_by_title.get(sub_title)
            for line in lines:
                entry = NoteEntryEntity(
                    id=line.target_id,
                    url=canonical_url(line.target_id),
                    title=line.title,
                    course=course.title if course else sub_title,
                    type=NOTE_TYPE_LABEL,
                    source=SOURCE_RECORDING_NOTES,
                    parent=subpage_id,
                )
                entries.append(entry)
                if course is not None:
                    by_course[course.id].append(entry)
                else:
                    course_unknown.append(entry)
        return entries, by_course, course_unknown, skipped_lines

    # -- exact-text row→course matching ----------------------------------------

    def _match_courses(self, courses: list[CourseEntity], material_entities):
        """Exact normalized-title equality only; NO fuzzy matching anywhere."""
        course_by_title = {course.title: course for course in courses}
        by_course: dict[str, list] = {course.id: [] for course in courses}
        course_unknown: list = []
        for material in material_entities:
            course = course_by_title.get(material.course)
            if course is not None:
                by_course[course.id].append(material)
            else:
                course_unknown.append(material)
        return by_course, course_unknown
