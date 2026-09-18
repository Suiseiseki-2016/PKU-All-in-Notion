"""The Notion metadata directory adapter against a fake workspace (VAL-META-001..009).

The fake mirrors the VERIFIED real shapes (library/notion-workspace.md) with
synthetic ids; assertions pin the binding adapter rules: semester-scoped hub
discovery, hub-children course list, tolerant lecture recognition,
schema-driven material reads with the exact serialization whitelist,
课堂录像笔记 line entries, 学习中心-as-sibling, exact-text course matching,
and stable id/URL identity with a local cache.
"""

from __future__ import annotations

import json

import pytest

import notion_meta_fake as fake

from pku_sync.notion_meta import IdentityCache, NotionDirectory
from pku_sync.notion_meta.errors import (
    HubAmbiguityError,
    HubNotFoundError,
    MaterialDatabaseNotFoundError,
)


def make_directory(ws, *, semester=fake.SEMESTER, cache_path=None):
    client = ws.client()
    directory = NotionDirectory(client, semester=semester, cache_path=cache_path)
    return client, directory


# -- VAL-META-001: hub discovery ---------------------------------------------


def test_hub_discovery_resolves_unique_current_semester_hub():
    ws = fake.build_verified_workspace()
    client, directory = make_directory(ws)
    data = directory.load()
    # exactly one current-semester hub resolved from the (fuzzy) search surface
    assert data.hub.id == fake.HUB
    assert data.hub.url == fake.notion_url(fake.HUB)
    assert data.hub.title == fake.HUB_TITLE
    assert data.semester == fake.SEMESTER
    # the older-semester typo hub ("Clash Notes 2026上半学期") coexists but
    # was excluded by semester scoping; one search, page-filtered
    assert ("search", "Class Notes", "page") in client.calls


def test_hub_discovery_ambiguity_lists_candidate_titles():
    ws = fake.build_verified_workspace()
    ws.search_results.append(fake.search_page(fake.AMBIG_HUB, "Class Notes 2026 下半学期（备份）"))
    _, directory = make_directory(ws)
    with pytest.raises(HubAmbiguityError) as err:
        directory.load()
    # the error lists candidate titles and never silently picks one
    assert err.value.candidates == [fake.HUB_TITLE, "Class Notes 2026 下半学期（备份）"]
    assert fake.HUB_TITLE in str(err.value)
    assert "Class Notes 2026 下半学期（备份）" in str(err.value)


def test_hub_discovery_missing_raises_explicit_error():
    ws = fake.build_verified_workspace()
    ws.search_results = []
    _, directory = make_directory(ws)
    with pytest.raises(HubNotFoundError) as err:
        directory.load()
    assert fake.SEMESTER in str(err.value)


def test_hub_discovery_default_semester_is_derived_from_date():
    ws = fake.build_verified_workspace()
    import datetime

    _, directory = make_directory(ws, semester=None)
    directory._now = datetime.datetime(2026, 9, 19)
    data = directory.load()
    assert data.semester == "2026 下半学期"
    assert data.hub.id == fake.HUB


# -- VAL-META-002/007: courses and the 学习中心 sibling ----------------------


def test_courses_exclude_specials_noise_and_databases():
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws)
    data = directory.load()
    # courses = direct hub children minus 学习中心/课堂录像笔记/noise/databases,
    # titles icon-stripped, nothing synthesized from any local course tree
    assert [c.title for c in data.courses] == ["计算机网络", "心理咨询与治疗引论", "认知心理学"]
    assert {c.id for c in data.courses} == {fake.COURSE_NET, fake.COURSE_PSY, fake.COURSE_COG}
    # course parents are the hub — 学习中心 is a SIBLING, never the parent
    assert all(c.parent == fake.HUB for c in data.courses)
    assert fake.LEARNING_CENTER not in {c.id for c in data.courses}
    assert fake.NOTES_HUB not in {c.id for c in data.courses}
    assert fake.DB_INDEX not in {c.id for c in data.courses}


def test_noise_children_surfaced_in_diagnostics():
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws)
    data = directory.load()
    assert data.diagnostics["excluded_hub_children"] == [
        "Blackboard 作业提交自动化经验（2026-09-12）"
    ]


def test_learning_center_is_sibling_semester_page_body_never_read():
    ws = fake.build_verified_workspace()
    client, directory = make_directory(ws)
    data = directory.load()
    assert data.semester_page is not None
    assert data.semester_page.id == fake.LEARNING_CENTER
    assert data.semester_page.title == "2026秋季学期学习中心"  # icon-stripped
    assert data.semester_page.url == fake.notion_url(fake.LEARNING_CENTER)
    # its body (每日检查记录 with local paths) is never read at all
    reads = [call for call in client.calls if call[1] == fake.LEARNING_CENTER]
    assert reads == []


# -- VAL-META-003: lectures -------------------------------------------------


def test_lectures_parse_all_variants_and_exclude_non_lecture_children():
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws)
    data = directory.load()
    net = data.lectures_by_course[fake.COURSE_NET]
    # all three verified title variants + the icon variant, in block order
    assert [l.id for l in net] == [fake.LECTURE1, fake.LECTURE2, fake.LECTURE3, fake.LECTURE4]
    assert [(l.number, l.date, l.period) for l in net] == [
        (1, "", ""),
        (2, "2026-09-09", "第1-2节"),
        (3, "2026-09-14", "第5-6节"),
        (2, "2026-09-16", "第1-2节"),
    ]
    # icon-stripped title on the icon variant
    assert net[3].title == "第二讲 · 计算机网络（2026-09-16 第1-2节）"
    assert all(l.parent == fake.COURSE_NET for l in net)
    # 《课程总结》 is a course child but never typed as a lecture
    assert fake.COURSE_SUMMARY not in {l.id for l in data.lectures}
    # a course with no lecture pages is a normal state
    assert data.lectures_by_course[fake.COURSE_COG] == []


# -- VAL-META-004/005/008: materials + matching ------------------------------


def test_material_schema_fetched_before_rows():
    ws = fake.build_verified_workspace()
    client, directory = make_directory(ws)
    directory.load()
    db_calls = [call for call in client.calls if call[0] in ("get_database", "query_database")]
    # schema first, then rows; the other database is never read
    assert db_calls == [
        ("get_database", fake.DB_INDEX),
        ("query_database", fake.DB_INDEX),
    ]


def test_material_rows_match_courses_by_exact_normalized_title():
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws)
    data = directory.load()
    by_net = data.materials_by_course[fake.COURSE_NET]
    # exact value + whitespace variant (full-width spaces) both match
    assert [m.id for m in by_net] == [fake.ROWS[0], fake.ROWS[1]]
    assert all(m.course == "计算机网络" for m in by_net)
    # icon-prefixed row value normalizes to the plain course title
    by_psy = data.materials_by_course[fake.COURSE_PSY]
    assert [m.id for m in by_psy] == [fake.ROWS[2]]
    assert by_psy[0].course == "心理咨询与治疗引论"
    by_cog = data.materials_by_course[fake.COURSE_COG]
    assert [m.id for m in by_cog] == [fake.ROWS[4]]


def test_unmatched_rows_retained_as_course_unknown():
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws)
    data = directory.load()
    unknown_ids = [m.id for m in data.course_unknown_materials]
    # unknown course value and empty course value both stay course-unknown
    assert unknown_ids == [fake.ROWS[3], fake.ROWS[5], fake.ROWS[6]]
    assert data.diagnostics["course_unknown_materials"] == 3
    # retained in the flat material list, excluded from course views
    assert set(unknown_ids) <= {m.id for m in data.materials}
    course_view_ids = {
        m.id for rows in data.materials_by_course.values() for m in rows
    }
    assert not set(unknown_ids) & course_view_ids


def test_no_fuzzy_course_matching():
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws)
    data = directory.load()
    # "计算机网络（本科）" is similar to 计算机网络 but NOT equal → never matched
    by_net = data.materials_by_course[fake.COURSE_NET]
    assert fake.ROWS[6] not in {m.id for m in by_net}
    assert fake.ROWS[6] in {m.id for m in data.course_unknown_materials}


def test_material_database_missing_raises_explicit_error():
    ws = fake.build_verified_workspace()
    ws.children[fake.HUB] = [
        block
        for block in ws.children[fake.HUB]
        if not (block.get("type") == "child_database" and block["id"] == fake.DB_INDEX)
    ]
    _, directory = make_directory(ws)
    with pytest.raises(MaterialDatabaseNotFoundError) as err:
        directory.load()
    assert "课程资料索引" in str(err.value)
    # lists what WAS found so the user can act; never a silent empty directory
    assert "2026秋季学习任务" in str(err.value)


# -- VAL-META-006: 课堂录像笔记 note entries ---------------------------------


def test_note_lines_parse_into_lecture_linked_entries():
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws)
    data = directory.load()
    net_notes = data.notes_by_course[fake.COURSE_NET]
    # URL form + native page-mention form both resolve to lecture identity
    assert [n.id for n in net_notes] == [fake.LECTURE1, fake.LECTURE2]
    first = net_notes[0]
    assert first.url == fake.notion_url(fake.LECTURE1)
    assert first.title == "第一讲 · 计算机网络（2026-09-07 第5-6节）"
    assert first.course == "计算机网络"
    assert first.type == "课堂笔记"  # synthesized label
    assert first.source == "课堂录像笔记"
    assert first.parent == fake.NOTES_SUB_NET
    # the mention-form line also carries the target lecture identity
    assert net_notes[1].id == fake.LECTURE2
    assert net_notes[1].url == fake.notion_url(fake.LECTURE2)


def test_course_without_notes_subpage_yields_zero_entries_no_error():
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws)
    data = directory.load()  # 认知心理学 has no 课堂录像笔记 subpage — no error
    assert data.notes_by_course.get(fake.COURSE_COG, []) == []
    # exactly the two courses with subpages produced entries
    assert len(data.notes) == 3


def test_unresolvable_note_lines_skipped_and_counted():
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws)
    data = directory.load()
    assert data.diagnostics["skipped_note_lines"] == 1
    assert {n.id for n in data.notes} == {
        fake.LECTURE1,
        fake.LECTURE2,
        fake.LECTURE_PSY3,
    }


# -- VAL-META-009: identity + cache ------------------------------------------


def test_every_entity_carries_id_and_canonical_url():
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws)
    data = directory.load()
    dump = data.to_dict()

    def assert_identity(entities):
        for entity in entities:
            assert entity["id"]
            assert entity["url"].startswith("https://www.notion.so/")
            assert entity["url"].endswith(entity["id"].replace("-", ""))

    for group in ("hub", "semester_page", "notes_hub", "material_database"):
        if dump[group]:
            assert_identity([dump[group]])
    assert_identity(dump["courses"])
    assert_identity(dump["lectures"])
    assert_identity(dump["materials"])
    assert_identity(dump["notes"])


def test_repeated_reads_return_identical_results():
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws)
    first = directory.load().to_dict()
    second = directory.load().to_dict()
    assert first == second


def test_identity_cache_written_and_readable_without_search(tmp_path):
    ws = fake.build_verified_workspace()
    cache_path = tmp_path / "notion" / "identity_cache.json"
    _, directory = make_directory(ws, cache_path=cache_path)
    directory.load()
    assert cache_path.exists()
    # the cache is a pure local file: launches resolve from it without any
    # workspace search
    cache = IdentityCache(cache_path)
    cached = cache.load()
    assert cached["hub"]["id"] == fake.HUB
    assert cached["semester"] == fake.SEMESTER
    assert cached["semester_page"]["id"] == fake.LEARNING_CENTER
    assert cached["notes_hub"]["id"] == fake.NOTES_HUB
    assert cached["material_database"]["id"] == fake.DB_INDEX
    assert {c["id"] for c in cached["courses"]} == {
        fake.COURSE_NET,
        fake.COURSE_PSY,
        fake.COURSE_COG,
    }
    # a second load refreshes the cache with the same identities
    directory.load()
    after = cache.load()
    before_no_stamp = {k: v for k, v in cached.items() if k != "saved_at"}
    after_no_stamp = {k: v for k, v in after.items() if k != "saved_at"}
    assert before_no_stamp == after_no_stamp


def test_identity_cache_tolerates_missing_or_corrupt_file(tmp_path):
    assert IdentityCache(tmp_path / "missing.json").load() is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert IdentityCache(bad).load() is None
    wrong = tmp_path / "wrong-version.json"
    wrong.write_text(json.dumps({"version": 999}), encoding="utf-8")
    assert IdentityCache(wrong).load() is None


# -- honeypot leak scan --------------------------------------------------------


def test_full_payload_excludes_forbidden_strings():
    """Signed S3 URLs, local paths, 来源路径/备注 values, transcript previews,
    and the client token never cross the adapter boundary."""
    ws = fake.build_verified_workspace()
    _, directory = make_directory(ws, cache_path=None)
    data = directory.load()
    dump = json.dumps(data.to_dict(), ensure_ascii=False)
    for needle in fake.FORBIDDEN_STRINGS:
        assert needle not in dump
