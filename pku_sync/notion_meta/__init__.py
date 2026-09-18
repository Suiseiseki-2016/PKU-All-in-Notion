"""Notion metadata directory adapter (M2).

Reads the connected workspace and returns metadata-only entities with stored
page identity: semester-scoped hub discovery, hub-children course list,
tolerant lecture recognition, schema-driven 课程资料索引 reads, and
课堂录像笔记 link lines. Never returns body text, 来源路径, 备注, media
URLs, or local paths (library/notion-workspace.md rules 1–10).
"""

from .cache import IdentityCache, default_cache_path
from .directory import NotionDirectory
from .entities import (
    ASSOCIATION_CONFIRMED,
    ASSOCIATION_NONE,
    ASSOCIATION_PENDING,
    NOTE_TYPE_LABEL,
    SOURCE_MATERIAL_INDEX,
    SOURCE_RECORDING_NOTES,
    Association,
    canonical_url,
    CourseEntity,
    DirectoryData,
    HubInfo,
    LectureEntity,
    LectureRef,
    MaterialEntity,
    NoteEntryEntity,
    normalize_page_id,
    PageRef,
)
from .errors import (
    HubAmbiguityError,
    HubNotFoundError,
    MaterialDatabaseNotFoundError,
    NotionMetaError,
    SchemaError,
)
from .materials import STATUS_VALUES, TYPE_VALUES
from .notes import NoteLine, parse_note_lines
from .titles import (
    LectureTitle,
    current_semester_label,
    is_hub_family,
    is_learning_center,
    is_noise_child,
    is_recording_notes_hub,
    matches_semester,
    normalize_title,
    parse_lecture_title,
    strip_icon,
)

__all__ = [
    "NotionDirectory",
    "DirectoryData",
    "IdentityCache",
    "default_cache_path",
    "HubInfo",
    "PageRef",
    "CourseEntity",
    "LectureEntity",
    "LectureRef",
    "MaterialEntity",
    "NoteEntryEntity",
    "Association",
    "NoteLine",
    "parse_note_lines",
    "LectureTitle",
    "current_semester_label",
    "is_hub_family",
    "is_learning_center",
    "is_noise_child",
    "is_recording_notes_hub",
    "matches_semester",
    "normalize_title",
    "strip_icon",
    "parse_lecture_title",
    "canonical_url",
    "normalize_page_id",
    "ASSOCIATION_CONFIRMED",
    "ASSOCIATION_PENDING",
    "ASSOCIATION_NONE",
    "NOTE_TYPE_LABEL",
    "SOURCE_MATERIAL_INDEX",
    "SOURCE_RECORDING_NOTES",
    "STATUS_VALUES",
    "TYPE_VALUES",
    "NotionMetaError",
    "HubNotFoundError",
    "HubAmbiguityError",
    "SchemaError",
    "MaterialDatabaseNotFoundError",
]
