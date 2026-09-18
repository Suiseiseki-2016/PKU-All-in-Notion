"""Exercise/grading-record reads are metadata-only (VAL-EXER-037) + the
complete-output honeypot scan (VAL-META-013).

Exercise pages (course-page children with exercise-family titles) and their
grading markers surface as METADATA ONLY — id, title, state signals (marker
presence, answer-presence), updated time — under a pinned allowlist.
Question text, answer text, explanations, and rubrics never cross the
adapter boundary: the body scan extracts booleans only, proven by honeypot
strings seeded into every body.
"""

from __future__ import annotations

import json

import notion_meta_fake as fake

from pku_sync.notion_meta import (
    LaunchResolver,
    NotionDirectory,
    exercises as exercises_mod,
)

# -- local exercise honeypots (synthetic content mirroring real body shapes) ---

EXERCISE_GRADED = fake.synth(401)  # 计算机网络 course child, production 批改结果 marker
EXERCISE_E2E_ANSWERED = fake.synth(402)  # [E2E] answered fixture, no grading marker yet
EXERCISE_FRESH = fake.synth(403)  # 心理咨询与治疗引论 course child, questions only
EXERCISE_E2E_GRADED = fake.synth(404)  # [E2E] graded via E2E_GRADE_RESULT marker
LECTURE_LIKE = fake.synth(405)  # "第四讲 课堂练习集" — lecture-titled, NOT an exercise

QUESTION_TEXT = "HONEYPOT题目：下列哪项不是分组交换的优点？"
# answer bullets must start with the pinned 答案： prefix (writer convention)
ANSWER_TEXT = "答案：B 独占信道时延更小（HONEYPOT学生答案）"
TEACHER_ANSWER_TEXT = "答案：A 资源利用率更高（HONEYPOT标准答案）"
EXPLANATION_TEXT = "HONEYPOT解析：分组交换的优点是资源利用率高、无需建立连接。"
RUBRIC_TEXT = "HONEYPOT评分标准：答对得 2 分，答错 0 分。"
E2E_ANSWER_LINE = "deliberately-wrong-answer-2"

# Marker strings are body content too: only their PRESENCE booleans may cross.
BODY_MARKER_STRINGS = ("批改结果", "E2E_GRADE_RESULT", "E2E_ANSWER_FIXTURE")

EXERCISE_HONEYPOTS = (
    QUESTION_TEXT,
    ANSWER_TEXT,
    TEACHER_ANSWER_TEXT,
    EXPLANATION_TEXT,
    RUBRIC_TEXT,
    E2E_ANSWER_LINE,
    *BODY_MARKER_STRINGS,
)


def heading(text: str, level: int = 2) -> dict:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": [fake.text_piece(text)]}}


def bullet(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [fake.text_piece(text)]},
    }


def numbered(text: str) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": [fake.text_piece(text)]},
    }


def exercise_page(page_id: str, *, title: str, parent: str, updated: str) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "url": fake.notion_url(page_id),
        "last_edited_time": updated,
        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
        "parent": {"type": "page_id", "page_id": parent},
    }


def build_exercise_workspace():
    ws = fake.build_verified_workspace()
    # 计算机网络 children gain exercise pages (and a lecture-titled child
    # that mentions 练习 — lecture recognition wins, it is never an exercise)
    ws.children[fake.COURSE_NET].extend(
        [
            fake.child_page_block(EXERCISE_GRADED, "计算机网络：第一讲随堂练习"),
            fake.child_page_block(EXERCISE_E2E_ANSWERED, "[E2E] 计算机网络 考前练习"),
            fake.child_page_block(LECTURE_LIKE, "第四讲 课堂练习集"),
        ]
    )
    ws.children[fake.COURSE_PSY].extend(
        [
            fake.child_page_block(EXERCISE_FRESH, "心理咨询与治疗引论 小测（第三讲）"),
            fake.child_page_block(EXERCISE_E2E_GRADED, "[E2E] 心理咨询与治疗引论 小测"),
        ]
    )
    # graded production exercise: question + answer bullets + 批改结果 section
    ws.children[EXERCISE_GRADED] = [
        fake.paragraph(QUESTION_TEXT),
        bullet(ANSWER_TEXT),
        bullet(TEACHER_ANSWER_TEXT),
        bullet(f"资料来源：{fake.SIGNED_S3_URL} {fake.LOCAL_PATH}"),
        heading("批改结果"),
        numbered("总分：4/8"),
        numbered(EXPLANATION_TEXT),
        numbered(RUBRIC_TEXT),
    ]
    ws.pages[EXERCISE_GRADED] = exercise_page(
        EXERCISE_GRADED,
        title="计算机网络：第一讲随堂练习",
        parent=fake.COURSE_NET,
        updated="2026-09-18T10:00:00.000Z",
    )
    # answered [E2E] exercise: question + the pinned answer-fixture area, no marker
    ws.children[EXERCISE_E2E_ANSWERED] = [
        fake.paragraph(QUESTION_TEXT),
        fake.paragraph(f"本地草稿路径：{fake.TRANSCRIPT_PREVIEW[:6]}…{fake.LOCAL_PATH}"),
        heading("E2E_ANSWER_FIXTURE"),
        numbered("1. answer-1"),
        numbered(f"2. {E2E_ANSWER_LINE}"),
    ]
    ws.pages[EXERCISE_E2E_ANSWERED] = exercise_page(
        EXERCISE_E2E_ANSWERED,
        title="[E2E] 计算机网络 考前练习",
        parent=fake.COURSE_NET,
        updated="2026-09-18T11:00:00.000Z",
    )
    # fresh exercise: question honeypots only — no marker, no answers
    ws.children[EXERCISE_FRESH] = [
        fake.paragraph(QUESTION_TEXT),
        bullet(f"来源讲次页：{fake.notion_url(fake.LECTURE_PSY3)}"),
    ]
    ws.pages[EXERCISE_FRESH] = exercise_page(
        EXERCISE_FRESH,
        title="心理咨询与治疗引论 小测（第三讲）",
        parent=fake.COURSE_PSY,
        updated="2026-09-18T12:00:00.000Z",
    )
    # graded [E2E] exercise via the E2E marker, answer area long gone
    ws.children[EXERCISE_E2E_GRADED] = [
        heading("E2E_GRADE_RESULT"),
        numbered(EXPLANATION_TEXT),
        numbered(RUBRIC_TEXT),
    ]
    ws.pages[EXERCISE_E2E_GRADED] = exercise_page(
        EXERCISE_E2E_GRADED,
        title="[E2E] 心理咨询与治疗引论 小测",
        parent=fake.COURSE_PSY,
        updated="2026-09-18T13:00:00.000Z",
    )
    return ws


def make_data(ws=None):
    ws = ws or build_exercise_workspace()
    return NotionDirectory(ws.client(), semester=fake.SEMESTER).load()


def exercise_by_id(data, page_id):
    return next(ex for ex in data.exercises if ex.id == page_id)


# -- VAL-EXER-037: metadata-only surface under a pinned allowlist ----------------


def test_exercise_reads_surface_metadata_only_exact_allowlist():
    data = make_data()
    graded = exercise_by_id(data, EXERCISE_GRADED).model_dump()
    assert set(graded) == {
        "id",
        "url",
        "title",
        "course",
        "parent",
        "updated",
        "marker_present",
        "answer_present",
    }
    assert graded["id"] == EXERCISE_GRADED
    assert graded["url"] == fake.notion_url(EXERCISE_GRADED)
    assert graded["title"] == "计算机网络：第一讲随堂练习"
    assert graded["course"] == "计算机网络"
    assert graded["parent"] == fake.COURSE_NET
    assert graded["updated"] == "2026-09-18T10:00:00.000Z"


def test_state_signals_marker_presence_and_answer_presence():
    data = make_data()
    # graded production page: 批改结果 marker + 答案： blocks
    graded = exercise_by_id(data, EXERCISE_GRADED)
    assert graded.marker_present is True
    assert graded.answer_present is True
    # answered but not graded: E2E answer fixture present, no marker yet
    answered = exercise_by_id(data, EXERCISE_E2E_ANSWERED)
    assert answered.marker_present is False
    assert answered.answer_present is True
    # fresh organize: questions only, nothing submitted
    fresh = exercise_by_id(data, EXERCISE_FRESH)
    assert fresh.marker_present is False
    assert fresh.answer_present is False
    # graded via the E2E marker with no answer area left
    e2e_graded = exercise_by_id(data, EXERCISE_E2E_GRADED)
    assert e2e_graded.marker_present is True
    assert e2e_graded.answer_present is False


def test_exercises_grouped_by_course():
    data = make_data()
    net_ids = {ex.id for ex in data.exercises_by_course[fake.COURSE_NET]}
    psy_ids = {ex.id for ex in data.exercises_by_course[fake.COURSE_PSY]}
    assert net_ids == {EXERCISE_GRADED, EXERCISE_E2E_ANSWERED}
    assert psy_ids == {EXERCISE_FRESH, EXERCISE_E2E_GRADED}
    assert {ex.id for ex in data.exercises} == net_ids | psy_ids


def test_non_exercise_children_never_typed_as_exercises():
    """《课程总结》 stays out, and a lecture-titled child that mentions 练习
    is a lecture (lecture recognition wins), never an exercise."""
    data = make_data()
    exercise_ids = {ex.id for ex in data.exercises}
    assert fake.COURSE_SUMMARY not in exercise_ids
    assert LECTURE_LIKE not in exercise_ids
    assert LECTURE_LIKE in {lec.id for lec in data.lectures_by_course[fake.COURSE_NET]}
    # the 认知心理学 course has no exercise children — a normal empty state
    assert data.exercises_by_course[fake.COURSE_COG] == []


# -- the pinned marker scan, unit level -----------------------------------------


def test_scan_exercise_signals_marker_variants():
    assert exercises_mod.scan_exercise_signals([heading("批改结果")]) == (True, False)
    assert exercises_mod.scan_exercise_signals([heading("E2E_GRADE_RESULT")]) == (True, False)
    # level-tolerant, text-exact
    assert exercises_mod.scan_exercise_signals([heading("批改结果", level=3)]) == (True, False)
    # a versioned heading is NOT the marker (exact text, no timestamp/version)
    assert exercises_mod.scan_exercise_signals([heading("批改结果（第2轮）")]) == (False, False)
    assert exercises_mod.scan_exercise_signals([fake.paragraph("批改结果")]) == (False, False)


def test_scan_exercise_signals_answer_variants():
    assert exercises_mod.scan_exercise_signals([heading("E2E_ANSWER_FIXTURE")]) == (False, True)
    assert exercises_mod.scan_exercise_signals([fake.paragraph("答案：B")]) == (False, True)
    assert exercises_mod.scan_exercise_signals([bullet("答案：存储转发")]) == (False, True)
    assert exercises_mod.scan_exercise_signals([numbered("答案：对")]) == (False, True)
    # a bare 答案： prefix with no content is NOT a presence signal
    assert exercises_mod.scan_exercise_signals([fake.paragraph("答案：")]) == (False, False)
    # plain numbered content without the pinned answer markers is not a signal
    assert exercises_mod.scan_exercise_signals([numbered("1. answer-1")]) == (False, False)


def test_marker_constants_are_pinned():
    assert exercises_mod.GRADING_MARKERS == ("批改结果", "E2E_GRADE_RESULT")
    assert exercises_mod.ANSWER_AREA_MARKERS == ("E2E_ANSWER_FIXTURE",)


# -- VAL-META-013: the COMPLETE serialized adapter output is honeypot-clean -----


def test_complete_serialized_output_excludes_all_forbidden_strings():
    """Full-dump substring scan over the complete adapter output — directory
    data (incl. exercises, associations, diagnostics) AND launch results —
    against the forbidden-string list from library/notion-workspace.md plus
    the exercise/grading honeypots. Question text, answer text, explanations,
    rubrics, marker strings, media/signed S3 URLs, local paths, 来源路径
    values, 备注 values, and tokens never cross the adapter boundary."""
    data = make_data()
    resolver = LaunchResolver(data)
    parts = [
        json.dumps(data.to_dict(), ensure_ascii=False),
        json.dumps(resolver.resolve(EXERCISE_GRADED).model_dump(), ensure_ascii=False),
        json.dumps(resolver.resolve(fake.LECTURE1).model_dump(), ensure_ascii=False),
        json.dumps(
            resolver.resolve("not-a-page-id", course_id=fake.COURSE_NET).model_dump(),
            ensure_ascii=False,
        ),
    ]
    dump = "\n".join(parts)
    needles = (*fake.FORBIDDEN_STRINGS, *EXERCISE_HONEYPOTS)
    for needle in needles:
        assert needle not in dump, f"forbidden string leaked: {needle!r}"
    # the surfaced signals are booleans only: identity + booleans + updated
    graded = exercise_by_id(data, EXERCISE_GRADED).model_dump()
    assert graded["marker_present"] is True and graded["answer_present"] is True
