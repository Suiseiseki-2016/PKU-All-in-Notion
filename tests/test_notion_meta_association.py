"""Association = explicit stored identity link ONLY (VAL-META-011/012).

Fixture matrix over the verified 课程资料索引 shape: explicit link (relation
property, URL property, canonical lecture URL in 备注 — each consumed only
when present in the fetched schema), title-pattern inference → 待确认,
explicit link overriding conflicting inference, and neither → course-level
(no association). Association state is structurally independent of 处理状态
(2×2). 备注 content is READ to consume an explicit lecture URL but never
serialized.
"""

from __future__ import annotations

import json

import notion_meta_fake as fake

from pku_sync.notion_meta import (
    ASSOCIATION_CONFIRMED,
    ASSOCIATION_NONE,
    ASSOCIATION_PENDING,
    NotionDirectory,
)


# -- local matrix fixture (synthetic ids, verified row shape) -------------------

R_REL = fake.synth(321)  # explicit relation property → 第三讲
R_URL = fake.synth(322)  # explicit URL property → 第二讲(2026-09-09)
R_REMARKS = fake.synth(323)  # canonical lecture URL in 备注 → 第一讲
R_INFER = fake.synth(324)  # title-pattern inference → 第二讲
R_CONFLICT = fake.synth(325)  # explicit relation → 第三讲 vs title pattern → 第一讲
R_NONE = fake.synth(326)  # no link, no pattern
R_PEND_LINKED = fake.synth(327)  # 处理状态=待确认 + explicit link (2×2)
R_PEND_UNLINKED = fake.synth(328)  # 处理状态=待确认, no link (2×2)
R_NONLECT_URL = fake.synth(329)  # 备注 URL that is NOT a lecture page
R_NONLECT_REL = fake.synth(330)  # relation to a non-lecture page
R_UNKNOWN_COURSE = fake.synth(331)  # 第N讲 title but unknown course
STALE_NOTE_TARGET = fake.synth(999)  # note line target that is not a lecture

REMARKS_WITH_LECTURE_URL = (
    f"讲次页面：{fake.notion_url(fake.LECTURE1)}；另附本地来源：{fake.LOCAL_PATH}"
)
REMARKS_WITH_NON_LECTURE_URL = f"参考链接：{fake.notion_url(fake.NOISE_PAGE)} 和 {fake.SIGNED_S3_URL}"


def relation_row(row_id: str, *, title: str, course: str, status: str, target: str) -> dict:
    row = fake.material_row(row_id, title=title, course=course, type_="讲义", status=status)
    row["properties"]["讲次"] = {"type": "relation", "relation": [{"id": target}]}
    return row


def url_prop_row(row_id: str, *, title: str, course: str, status: str, url: str) -> dict:
    row = fake.material_row(row_id, title=title, course=course, type_="复习资料", status=status)
    row["properties"]["讲次链接"] = {"type": "url", "url": url}
    return row


def remarks_row(row_id: str, *, title: str, course: str, status: str, remarks: str) -> dict:
    return fake.material_row(
        row_id, title=title, course=course, type_="课堂课件", status=status, remarks=remarks
    )


def build_matrix_workspace():
    """Verified schema extended with forward-compatible link channels
    (relation/URL properties are consumed only when the schema declares them)."""
    ws = fake.build_verified_workspace()
    schema = fake.material_schema()
    schema["properties"]["讲次"] = {"type": "relation", "relation": {}}
    schema["properties"]["讲次链接"] = {"type": "url", "url": {}}
    ws.databases[fake.DB_INDEX] = schema
    ws.rows[fake.DB_INDEX] = [
        relation_row(R_REL, title="课堂重点整理.pdf", course="计算机网络", status="已索引", target=fake.LECTURE3),
        url_prop_row(R_URL, title="网络层小结.pdf", course="计算机网络", status="待阅读", url=fake.notion_url(fake.LECTURE2)),
        remarks_row(R_REMARKS, title="课程介绍提纲.pdf", course="计算机网络", status="已索引", remarks=REMARKS_WITH_LECTURE_URL),
        fake.material_row(R_INFER, title="第二讲（2）数据链路层.pdf", course="计算机网络", type_="讲义", status="待阅读"),
        relation_row(R_CONFLICT, title="第一讲（1）课程介绍复习.pdf", course="计算机网络", status="重点", target=fake.LECTURE3),
        fake.material_row(R_NONE, title="计算机网络教学大纲（本科）.pdf", course="计算机网络", type_="课程手册", status="已索引"),
        relation_row(R_PEND_LINKED, title="错题整理.pdf", course="计算机网络", status="待确认", target=fake.LECTURE3),
        fake.material_row(R_PEND_UNLINKED, title="待整理资料清单.pdf", course="计算机网络", type_="参考阅读", status="待确认"),
        remarks_row(R_NONLECT_URL, title="外部参考资料汇总.pdf", course="计算机网络", status="待阅读", remarks=REMARKS_WITH_NON_LECTURE_URL),
        relation_row(R_NONLECT_REL, title="其他课程关联.pdf", course="计算机网络", status="已索引", target=fake.NOISE_PAGE),
        fake.material_row(R_UNKNOWN_COURSE, title="第五讲量子力学习题.pdf", course="量子力学导论", type_="讲义", status="已索引"),
    ]
    # a 课堂录像笔记 line whose target resolves to a page that is NOT one of
    # the directory's lectures (stale link)
    ws.children[fake.NOTES_SUB_NET].append(
        fake.paragraph(
            f"《第五讲 · 计算机网络（2026-10-09 第3-4节）》→ {fake.notion_url(STALE_NOTE_TARGET)}"
        )
    )
    return ws


def make_directory(ws):
    return NotionDirectory(ws.client(), semester=fake.SEMESTER)


def association_of(data, row_id):
    material = next(m for m in data.materials if m.id == row_id)
    return material, material.association


# -- VAL-META-011: the explicit/inferred/conflict/none matrix ------------------


def test_explicit_relation_property_yields_confirmed_association():
    data = make_directory(build_matrix_workspace()).load()
    material, association = association_of(data, R_REL)
    assert association.state == ASSOCIATION_CONFIRMED
    assert association.lecture is not None
    assert association.lecture.id == fake.LECTURE3
    assert association.lecture.url == fake.notion_url(fake.LECTURE3)
    assert material.status == "已索引"


def test_explicit_url_property_yields_confirmed_association():
    data = make_directory(build_matrix_workspace()).load()
    _, association = association_of(data, R_URL)
    assert association.state == ASSOCIATION_CONFIRMED
    assert association.lecture.id == fake.LECTURE2


def test_canonical_lecture_url_in_remarks_yields_confirmed_association():
    data = make_directory(build_matrix_workspace()).load()
    material, association = association_of(data, R_REMARKS)
    assert association.state == ASSOCIATION_CONFIRMED
    assert association.lecture.id == fake.LECTURE1
    # the 备注 value (with its local-path fragment) never serializes
    assert REMARKS_WITH_LECTURE_URL not in json.dumps(material.model_dump(), ensure_ascii=False)


def test_title_pattern_inference_yields_pending_never_confirmed():
    data = make_directory(build_matrix_workspace()).load()
    material, association = association_of(data, R_INFER)
    assert association.state == ASSOCIATION_PENDING  # serialized as 待确认
    # inference resolves the lecture by (course, number): 计算机网络第二讲 —
    # two lectures share that number; the FIRST is the hint target
    assert association.lecture is not None
    assert association.lecture.id == fake.LECTURE2
    assert material.status == "待阅读"


def test_explicit_link_overrides_conflicting_inference():
    data = make_directory(build_matrix_workspace()).load()
    material, association = association_of(data, R_CONFLICT)
    # the title pattern points at 第一讲, the relation at 第三讲: explicit wins
    assert association.state == ASSOCIATION_CONFIRMED
    assert association.lecture.id == fake.LECTURE3
    # the conflicting inference is dropped entirely: no confirmed AND no
    # 待确认 association is emitted for 第一讲
    confirmed_l1 = data.confirmed_materials_for_lecture(fake.LECTURE1)
    inferred_l1 = data.inferred_materials_for_lecture(fake.LECTURE1)
    assert R_CONFLICT not in {m.id for m in confirmed_l1}
    assert R_CONFLICT not in {m.id for m in inferred_l1}


def test_neither_link_nor_pattern_yields_course_level_none():
    data = make_directory(build_matrix_workspace()).load()
    _, association = association_of(data, R_NONE)
    assert association.state == ASSOCIATION_NONE
    assert association.lecture is None


def test_non_lecture_links_never_confirm():
    """A URL/relation that does not resolve to a known lecture page is not an
    explicit lecture link (and the seeded S3 URL in 备注 never leaks)."""
    data = make_directory(build_matrix_workspace()).load()
    _, association_url = association_of(data, R_NONLECT_URL)
    _, association_rel = association_of(data, R_NONLECT_REL)
    assert association_url.state == ASSOCIATION_NONE
    assert association_rel.state == ASSOCIATION_NONE
    dump = json.dumps(data.to_dict(), ensure_ascii=False)
    assert fake.SIGNED_S3_URL not in dump


def test_inference_requires_a_known_course():
    """A 第N讲 title on a course-unknown row cannot infer: no lecture set."""
    data = make_directory(build_matrix_workspace()).load()
    material, association = association_of(data, R_UNKNOWN_COURSE)
    assert association.state == ASSOCIATION_NONE
    assert association.lecture is None
    assert material.id in {m.id for m in data.course_unknown_materials}


# -- confirmed queries exclude inference ---------------------------------------


def test_confirmed_query_returns_only_explicitly_linked_materials():
    data = make_directory(build_matrix_workspace()).load()
    confirmed_l3 = {m.id for m in data.confirmed_materials_for_lecture(fake.LECTURE3)}
    # exactly the explicit links: relation row, conflict row (explicit won),
    # and the 待确认-status row that is still explicitly linked
    assert confirmed_l3 == {R_REL, R_CONFLICT, R_PEND_LINKED}
    # the inferred 第二讲 row never appears in any confirmed query
    confirmed_l2 = {m.id for m in data.confirmed_materials_for_lecture(fake.LECTURE2)}
    assert confirmed_l2 == {R_URL}
    assert R_INFER not in confirmed_l2


def test_inferred_query_returns_pending_materials_only():
    data = make_directory(build_matrix_workspace()).load()
    inferred_l2 = {m.id for m in data.inferred_materials_for_lecture(fake.LECTURE2)}
    assert inferred_l2 == {R_INFER}
    assert R_URL not in inferred_l2  # confirmed is not inferred


# -- VAL-META-012: association state varies independently of 处理状态 ----------


def test_association_state_independent_of_processing_status_2x2():
    """A 处理状态=待确认 material with an explicit link is confirmed-linked
    and keeps its own status badge; all four quadrants stay distinct."""
    data = make_directory(build_matrix_workspace()).load()
    quadrants = {
        # (association state, 处理状态) for each fixture row
        R_PEND_LINKED: (ASSOCIATION_CONFIRMED, "待确认"),  # linked + pending status
        R_REL: (ASSOCIATION_CONFIRMED, "已索引"),  # linked + indexed status
        R_PEND_UNLINKED: (ASSOCIATION_NONE, "待确认"),  # unlinked + pending status
        R_NONE: (ASSOCIATION_NONE, "已索引"),  # unlinked + indexed status
    }
    for row_id, (expected_state, expected_status) in quadrants.items():
        material, association = association_of(data, row_id)
        assert association.state == expected_state
        assert material.status == expected_status
        # serialized independently: two distinct fields, four distinct combos
        dump = material.model_dump()
        assert dump["association"]["state"] == expected_state
        assert dump["status"] == expected_status
    # the linked+待确认 row is confirmed in the confirmed query despite its
    # own 待确认 processing badge
    assert R_PEND_LINKED in {m.id for m in data.confirmed_materials_for_lecture(fake.LECTURE3)}
    # ...and the unlinked+待确认 row is not
    assert R_PEND_UNLINKED not in {
        m.id for m in data.confirmed_materials_for_lecture(fake.LECTURE3)
    }


# -- 课堂录像笔记 channel (i): notes carry the explicit link ------------------


def test_note_entries_are_confirmed_linked_to_their_line_target():
    data = make_directory(build_matrix_workspace()).load()
    net_notes = data.notes_by_course[fake.COURSE_NET]
    by_target = {note.id: note for note in net_notes}
    # every line targeting a known lecture page is a confirmed association
    for target in (fake.LECTURE1, fake.LECTURE2):
        note = by_target[target]
        assert note.association.state == ASSOCIATION_CONFIRMED
        assert note.association.lecture is not None
        assert note.association.lecture.id == target


def test_note_line_to_a_non_lecture_target_is_pending_not_confirmed():
    """A stale link resolving to a page outside the lecture set is never
    confirmed; the entry keeps its stored identity but the association stays
    visibly 待确认."""
    data = make_directory(build_matrix_workspace()).load()
    stale = next(note for note in data.notes if note.id == STALE_NOTE_TARGET)
    assert stale.association.state == ASSOCIATION_PENDING
    assert stale.association.lecture is None
    # identity is the line target itself; course attribution is preserved
    assert stale.url == fake.notion_url(STALE_NOTE_TARGET)
    assert stale.course == "计算机网络"


# -- privacy: consumed link channels never serialize their raw values ----------


def test_full_matrix_payload_excludes_forbidden_strings():
    """备注 is read to consume an explicit lecture URL, but neither 备注
    values, 来源路径 values, local paths, signed URLs, nor tokens cross the
    adapter boundary (VAL-META-013 over the association matrix)."""
    data = make_directory(build_matrix_workspace()).load()
    dump = json.dumps(data.to_dict(), ensure_ascii=False)
    needles = (
        *fake.FORBIDDEN_STRINGS,
        REMARKS_WITH_LECTURE_URL,
        REMARKS_WITH_NON_LECTURE_URL,
        "讲次页面：",
        "参考链接：",
    )
    for needle in needles:
        assert needle not in dump, f"forbidden string leaked: {needle!r}"
    # the consumed lecture URL itself is legitimate identity (not 备注 text):
    # it appears only as the association's lecture ref url
    assert fake.notion_url(fake.LECTURE1) in dump
