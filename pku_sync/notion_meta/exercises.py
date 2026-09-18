"""Exercise-page metadata reads (VAL-EXER-037).

Exercise pages are course-page children with exercise-family titles (lecture
recognition runs first, so lecture-titled children are never typed as
exercises). Their bodies are scanned for PRESENCE SIGNALS only — grading
markers (已批改 signal) and answer areas (answer-presence) — under a pinned
marker vocabulary. Question text, answer text, explanations, and rubrics
never cross the adapter boundary: the scan extracts booleans and identity
only, proven by honeypot strings seeded in tests.
"""

from __future__ import annotations

from ..notion import page_url
from .entities import ExerciseEntity, canonical_url
from .titles import is_exercise_title, normalize_title, parse_lecture_title

# Pinned grading-marker headings (library/exercise-grading-design.md): a
# stable EXACT-TEXT heading — no timestamp/version inside the marker.
GRADING_MARKERS = ("批改结果", "E2E_GRADE_RESULT")
# Pinned answer-area markers: the [E2E] answer-fixture heading, plus the
# writer-convention answer blocks ("答案：…" with non-empty content).
ANSWER_AREA_MARKERS = ("E2E_ANSWER_FIXTURE",)

_ANSWER_PREFIX = "答案："
_HEADING_TYPES = ("heading_1", "heading_2", "heading_3")
_TEXT_TYPES = ("paragraph", "bulleted_list_item", "numbered_list_item")


def scan_exercise_signals(blocks: list[dict]) -> tuple[bool, bool]:
    """Body blocks → (marker_present, answer_present).

    Top-level blocks only (markers and answer areas are appended top-level by
    construction). Heading level is tolerated, marker text is exact.
    """
    marker_present = False
    answer_present = False
    for block in blocks:
        kind = block.get("type")
        inner = block.get(kind) or {}
        stripped = _plain_text(inner.get("rich_text")).strip()
        if kind in _HEADING_TYPES:
            if stripped in GRADING_MARKERS:
                marker_present = True
            if stripped in ANSWER_AREA_MARKERS:
                answer_present = True
        elif kind in _TEXT_TYPES and stripped.startswith(_ANSWER_PREFIX):
            if len(stripped) > len(_ANSWER_PREFIX):
                answer_present = True
    return marker_present, answer_present


def read_exercises(
    client, children_by_course: dict[str, list[dict]], courses: list
) -> tuple[list[ExerciseEntity], dict[str, list[ExerciseEntity]]]:
    """Exercise pages from already-fetched course children — metadata only."""
    exercises: list[ExerciseEntity] = []
    by_course: dict[str, list[ExerciseEntity]] = {course.id: [] for course in courses}
    for course in courses:
        for block in children_by_course.get(course.id, []):
            if block.get("type") != "child_page":
                continue  # body content is ignored, never captured
            raw_title = (block.get("child_page") or {}).get("title", "")
            if parse_lecture_title(raw_title) is not None:
                continue  # lecture recognition wins
            if not is_exercise_title(raw_title):
                continue
            page_id = block["id"]
            page = client.get_page(page_id)
            marker_present, answer_present = scan_exercise_signals(
                client.list_children(page_id)
            )
            url = (page.get("url") if page else "") or canonical_url(page_id)
            entity = ExerciseEntity(
                id=page_id,
                url=url,
                title=normalize_title(raw_title),
                course=course.title,
                parent=course.id,
                updated=str(page.get("last_edited_time", "")),
                marker_present=marker_present,
                answer_present=answer_present,
            )
            exercises.append(entity)
            by_course[course.id].append(entity)
    return exercises, by_course


def _plain_text(rich) -> str:
    if not rich:
        return ""
    return "".join(piece.get("plain_text", "") for piece in rich)
