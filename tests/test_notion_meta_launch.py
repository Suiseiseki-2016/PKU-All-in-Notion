"""Launch resolution uses stored identity only (VAL-META-014).

Every launch path (lecture, material, notes, course fallback) resolves from
stored page identity — never a workspace search, never a most-recent-page
fallback. A missing identity yields an explicit missing-mapping result with
a course-level fallback; the mock client's call log shows ZERO search calls
(and zero calls of any kind) across all launch paths.
"""

from __future__ import annotations

import notion_meta_fake as fake

from pku_sync.notion_meta import (
    LAUNCH_MISSING_MAPPING,
    LAUNCH_OPENED,
    LaunchResolver,
    NotionDirectory,
)

UNKNOWN_PAGE = fake.synth(777)  # a syntactically valid id that is not in the directory


def make_resolver():
    ws = fake.build_verified_workspace()
    client = ws.client()
    data = NotionDirectory(client, semester=fake.SEMESTER).load()
    client.calls.clear()  # snapshot AFTER discovery; launch paths must add nothing
    return client, LaunchResolver(data), data


def test_launch_paths_resolve_exactly_the_stored_identity():
    client, resolver, data = make_resolver()
    lecture = data.lectures_by_course[fake.COURSE_NET][0]
    material = data.materials_by_course[fake.COURSE_NET][0]
    note = data.notes_by_course[fake.COURSE_NET][0]

    lecture_result = resolver.resolve(lecture.id)
    material_result = resolver.resolve(material.id)
    note_result = resolver.resolve(note.id)  # note identity IS the lecture page id
    course_result = resolver.resolve(fake.COURSE_NET)  # the course fallback path

    for result, entity in (
        (lecture_result, lecture),
        (material_result, material),
        (note_result, note),
    ):
        assert result.status == LAUNCH_OPENED
        assert result.url == entity.url  # exactly the stored identity URL
    assert course_result.status == LAUNCH_OPENED
    assert course_result.url == fake.notion_url(fake.COURSE_NET)


def test_launch_accepts_equivalent_id_forms_but_never_guesses():
    client, resolver, _ = make_resolver()
    hex32_form = fake.LECTURE1.replace("-", "")
    result = resolver.resolve(hex32_form)
    assert result.status == LAUNCH_OPENED
    assert result.url == fake.notion_url(fake.LECTURE1)


def test_missing_identity_yields_missing_mapping_with_course_fallback():
    client, resolver, _ = make_resolver()
    result = resolver.resolve(UNKNOWN_PAGE, course_id=fake.COURSE_NET)
    assert result.status == LAUNCH_MISSING_MAPPING
    assert result.url is None  # never a fabricated or guessed URL
    assert result.fallback is not None
    assert result.fallback.id == fake.COURSE_NET
    assert result.fallback.url == fake.notion_url(fake.COURSE_NET)
    assert result.fallback.title == "计算机网络"


def test_missing_identity_without_course_context_has_no_fallback():
    client, resolver, _ = make_resolver()
    result = resolver.resolve(UNKNOWN_PAGE)
    assert result.status == LAUNCH_MISSING_MAPPING
    assert result.url is None
    assert result.fallback is None


def test_garbage_target_is_missing_mapping_not_a_crash():
    client, resolver, _ = make_resolver()
    result = resolver.resolve("not-a-page-id")
    assert result.status == LAUNCH_MISSING_MAPPING
    assert result.url is None


def test_zero_client_calls_across_all_launch_paths():
    """VAL-META-014: the mock client's call log shows ZERO search calls (and
    zero calls of any kind) across lecture, material, notes, and course
    fallback launches, including missing-identity resolutions."""
    client, resolver, _ = make_resolver()
    for target in (
        fake.LECTURE1,
        fake.ROWS[0],
        fake.LECTURE2,  # the notes channel target
        fake.COURSE_NET,  # course fallback path
        UNKNOWN_PAGE,  # missing identity → missing mapping, still no search
        "not-a-page-id",
    ):
        resolver.resolve(target)
        resolver.resolve(target, course_id=fake.COURSE_NET)
    assert client.calls == []
    search_calls = [call for call in client.calls if call[0] == "search"]
    assert search_calls == []


def test_launch_result_serializes_to_exact_allowlist():
    client, resolver, _ = make_resolver()
    opened = resolver.resolve(fake.LECTURE1).model_dump()
    missing = resolver.resolve(UNKNOWN_PAGE, course_id=fake.COURSE_NET).model_dump()
    assert set(opened) == {"status", "target_id", "url", "fallback"}
    assert set(missing) == {"status", "target_id", "url", "fallback"}
    assert opened["fallback"] is None
    assert missing["fallback"] == {
        "id": fake.COURSE_NET,
        "url": fake.notion_url(fake.COURSE_NET),
        "title": "计算机网络",
    }
