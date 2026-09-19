"""The three material views' full semantics (VAL-META-025/026/027/028/034).

The 按讲次 / 按资料类型 / 全部资料 views are rendered by ``static/app.js``
from the directory API's payloads and ``window.PANEL_COPY``. These tests pin
the three halves that make the browser assertions meaningful:

- the data half: the seeded fixtures cover the three-way 按讲次
  classification (mapped ⇔ ≥1 CONFIRMED link; inferred-only; zero materials)
  and every view payload carries the fields the per-row label derivation
  needs — an explicit link to a DIFFERENT lecture must never render
  已关联本讲, and association state must stay independent of 处理状态;
- the copy half: every string the views render is pinned against the
  approved prototype (docs/UI_DESIGN.html materialsListHtml);
- the renderer half: the shipped assets implement the pinned semantics —
  the empty copy is reachable for a zero-materials lecture, the linked
  label is selected-lecture-relative, inferred associations and
  处理状态=待确认 both render with distinct pending classes (never the
  indexed green), and view switching re-renders in place without a reload.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import (
    COURSE_COG,
    COURSE_CS,
    COURSE_DEV,
    COURSE_NET,
    COG_L1,
    DEV_L1,
    NET_L1,
    NET_L2,
    NET_MISSING,
    NET_ROWS,
    PANEL_FORBIDDEN_STRINGS,
)
from pku_sync.panel.student_ui import LECTURE_COPY, STUDENT_UI_PATH
from pku_sync.panel.webapi import create_app

# The verified 资料类型 select options (library/notion-workspace.md) plus the
# two synthesized labels the views may legally show.
VERIFIED_TYPE_LABELS = {
    "课程手册", "讲义", "课堂课件", "复习资料", "往年题", "作业", "参考阅读", "平台说明",
    "课堂笔记",  # synthesized label for 课堂录像笔记 entries
    "未标注",  # empty 资料类型 display case
}


def make_client() -> TestClient:
    return TestClient(create_app(directory_service=build_fake_directory()))


def view(client: TestClient, course_id: str, kind: str, lecture_id: str | None = None) -> dict:
    params = {"view": kind}
    if lecture_id:
        params["lecture_id"] = lecture_id
    response = client.get(f"/api/courses/{course_id}/materials", params=params)
    assert response.status_code == 200
    return response.json()


def assets(client: TestClient) -> dict[str, str]:
    return {
        "js": client.get(f"{STUDENT_UI_PATH}/app.js").text,
        "css": client.get(f"{STUDENT_UI_PATH}/app.css").text,
    }


def js_function(text: str, name: str) -> str:
    """One top-level renderer function (app.js closes functions at 2-space
    indent, so the first ``\\n  }`` after the signature is the function end)."""
    start = text.index(f"function {name}(")
    end = text.index("\n  }", start) + len("\n  }")
    return text[start:end]


def css_block(text: str, selector: str) -> str:
    start = text.index(selector)
    return text[start : text.index("}", start) + 1]


# -- VAL-META-025: the three-way 按讲次 classification data ---------------------


def test_lecture_view_data_pins_the_three_way_classification():
    client = make_client()
    # mapped-with-materials AND mapped-with-inferred-subsection (第一讲):
    # one confirmed index row + one confirmed 课堂笔记 entry, one inferred row
    net1 = view(client, COURSE_NET, "lecture", NET_L1)
    assert net1["mapped"] is True
    assert len(net1["confirmed"]) == 2
    assert len(net1["inferred"]) == 1
    confirmed_sources = {item["source"] for item in net1["confirmed"]}
    assert confirmed_sources == {"课程资料索引", "课堂录像笔记"}
    for item in net1["confirmed"]:
        assert item["linked_to_lecture"] is True
        assert item["association_state"] == "confirmed"
        assert item["lecture"]["id"] == NET_L1  # an explicit link to THIS lecture
    inferred = net1["inferred"][0]
    assert inferred["title"] == "第一讲（2）基础知识.pdf"
    assert inferred["linked_to_lecture"] is False
    assert inferred["association_state"] == "待确认"  # never 已关联本讲 data
    assert inferred["lecture"]["id"] == NET_L1  # the hint target stays visible
    # inferred-only (unmapped) lectures: the mapping-unconfirmed panel data
    for course_id, lecture_id in ((COURSE_NET, NET_L2), (COURSE_DEV, DEV_L1)):
        body = view(client, course_id, "lecture", lecture_id)
        assert body["mapped"] is False
        assert body["confirmed"] == []
        assert len(body["inferred"]) == 1
        assert body["inferred"][0]["association_state"] == "待确认"
    # zero materials at all: the approved empty-copy data
    cog = view(client, COURSE_COG, "lecture", COG_L1)
    assert cog["mapped"] is False
    assert cog["confirmed"] == []
    assert cog["inferred"] == []


# -- VAL-META-026: type groups use the verified vocabulary with counts ----------


def test_type_view_groups_use_the_verified_vocabulary_with_counts():
    client = make_client()
    body = view(client, COURSE_NET, "type")
    labels = [group["type"] for group in body["groups"]]
    assert set(labels) <= VERIFIED_TYPE_LABELS
    assert "课堂笔记" in labels  # the synthesized label for 课堂录像笔记 entries
    for group in body["groups"]:
        assert set(group) == {"type", "count", "items"}
        assert group["count"] == len(group["items"])
    assert sum(group["count"] for group in body["groups"]) == 7
    # one group mixes both association kinds — labels stay preserved per row
    notes = next(group for group in body["groups"] if group["type"] == "课堂笔记")
    assert notes["count"] == 2
    states = {item["association_state"] for item in notes["items"]}
    assert states == {"confirmed", "待确认"}


# -- VAL-META-027: the flat 全部资料 list with per-row label data ---------------


def test_all_view_rows_carry_the_selected_lecture_relative_label_data():
    client = make_client()
    body = view(client, COURSE_NET, "all")
    items = body["items"]
    assert len(items) == 7  # the API's linked (2) + course-level (5)
    by_id = {item["id"]: item for item in items}
    # an explicit link to ANOTHER lecture exists in the pool: from 第二讲's
    # screen the renderer must NOT label it 已关联本讲 — the payload carries
    # both facts the comparison needs (linked + the linked lecture's id)
    other = by_id[NET_ROWS[0]]
    assert other["linked_to_lecture"] is True
    assert other["lecture"]["id"] == NET_L1
    assert other["lecture"]["id"] != NET_L2  # the discrimination is possible
    # the 待确认 note entry: association pending, no lecture, no 处理状态
    pending_note = by_id[NET_MISSING]
    assert pending_note["association_state"] == "待确认"
    assert pending_note["linked_to_lecture"] is False
    assert pending_note["is_note"] is True
    assert pending_note["status"] == ""
    assert pending_note["lecture"] is None
    # every row keeps stored identity for the 在 Notion 查看 action
    for item in items:
        assert item["url"] == f"https://www.notion.so/{item['id'].replace('-', '')}"
        assert item["type"] in VERIFIED_TYPE_LABELS


def test_all_view_row_count_equals_the_course_pool_for_every_course():
    client = make_client()
    directory = client.get("/api/directory").json()
    for course in directory["courses"]:
        flat = view(client, course["id"], "all")["items"]
        groups = view(client, course["id"], "type")["groups"]
        assert len(flat) == course["counts"]["materials"]
        assert sum(group["count"] for group in groups) == course["counts"]["materials"]


# -- VAL-META-034 (data): association state is independent of 处理状态 ----------


def test_association_state_and_processing_status_are_independent():
    client = make_client()
    dev = {item["title"]: item for item in view(client, COURSE_DEV, "all")["items"]}
    # inferred association + indexed processing status
    assert dev["第三讲发展心理学讲义.pdf"]["association_state"] == "待确认"
    assert dev["第三讲发展心理学讲义.pdf"]["status"] == "已索引"
    # no association + pending processing status
    assert dev["发展心理学课程大纲.pdf"]["association_state"] == "none"
    assert dev["发展心理学课程大纲.pdf"]["status"] == "待确认"
    net1 = view(client, COURSE_NET, "lecture", NET_L1)
    confirmed = {item["source"]: item for item in net1["confirmed"]}
    # confirmed link + indexed status, and confirmed link with no status chip
    assert confirmed["课程资料索引"]["association_state"] == "confirmed"
    assert confirmed["课程资料索引"]["status"] == "已索引"
    assert confirmed["课堂录像笔记"]["association_state"] == "confirmed"
    assert confirmed["课堂录像笔记"]["status"] == ""


def test_every_course_material_view_stays_honeypot_clean():
    client = make_client()
    blobs = []
    for course_id in (COURSE_NET, COURSE_DEV, COURSE_COG, COURSE_CS):
        blobs.append(json.dumps(view(client, course_id, "all"), ensure_ascii=False))
        blobs.append(json.dumps(view(client, course_id, "type"), ensure_ascii=False))
    blobs.append(json.dumps(view(client, COURSE_DEV, "lecture", DEV_L1), ensure_ascii=False))
    blobs.append(json.dumps(view(client, COURSE_COG, "lecture", COG_L1), ensure_ascii=False))
    for name, text in assets(client).items():
        for needle in PANEL_FORBIDDEN_STRINGS:
            if name == "css" and needle == "keyframes":
                # CSS's @keyframes at-rule is not the recordings' keyframes
                # directory: assert that is the only occurrence instead
                assert text.count("keyframes") == text.count("@keyframes")
                continue
            assert needle not in text, (name, needle)
    for blob in blobs:
        for needle in PANEL_FORBIDDEN_STRINGS:
            assert needle not in blob, needle
        assert "来源路径" not in blob and "备注" not in blob


# -- the copy half: prototype-verbatim strings -----------------------------------


def test_material_views_copy_is_pinned_against_the_prototype():
    copy = LECTURE_COPY["materials"]
    assert copy["title"] == "本讲相关资料"
    assert copy["note"] == "仅索引信息与来源 · 正文在 Notion 中查看"
    assert copy["controls_label"] == "资料查看方式"
    assert copy["views"] == {"lecture": "按讲次", "type": "按资料类型", "all": "全部资料"}
    assert copy["linked"] == "已关联本讲"
    assert copy["course_level"] == "课程级资料"
    assert copy["pending"] == "资料映射待确认"
    assert copy["pending_group"] == "待确认关联 · {count} 份"
    assert copy["group"] == "{type} · {count} 份"
    assert copy["source"] == "来源 · {source}"
    assert copy["open"] == "在 Notion 查看"
    assert copy["empty"] == {
        "title": "当前视图下暂无资料",
        "body": "可以切换「全部资料」查看课程级索引内容。",
    }
    assert copy["unmapped"]["title"] == "本讲资料映射尚未确认"
    assert copy["unmapped"]["body"] == (
        "目前无法从课程资料索引建立确定的讲次关联。你可以先查看课程级资料，"
        "不会把未确认的内容误当成本讲材料。"
    )
    assert copy["unmapped"]["action"] == "查看课程资料"
    assert copy["hint"] == (
        "提示：同一资料可能存在版本或重复项；此处只展示已索引的来源与状态，"
        "不暴露本地路径或资料正文。"
    )
    assert copy["loading"] == "正在载入资料索引"
    assert copy["error"] == "资料索引读取失败，请稍后重试。"


# -- the renderer half: the shipped assets implement the semantics --------------


def test_renderer_reaches_the_empty_copy_before_the_unmapped_panel():
    """A zero-materials lecture shows the approved empty copy, NOT the
    mapping-unconfirmed panel (that panel is for inferred-only lectures)."""
    body = js_function(assets(make_client())["js"], "materialsBody")
    lecture_branch = body[body.index('payload.view === "lecture"'):]
    assert 'data-state="empty"' in lecture_branch
    assert 'data-state="unmapped"' in lecture_branch
    # the zero-materials guard must run BEFORE the mapped guard, otherwise the
    # empty copy is unreachable (mapped ⇔ confirmed ≥ 1)
    assert "!confirmed.length && !inferred.length" in lecture_branch
    assert "!payload.mapped" in lecture_branch
    assert (
        lecture_branch.index("!confirmed.length && !inferred.length")
        < lecture_branch.index("!payload.mapped")
    )


def test_renderer_labels_linked_rows_only_for_the_selected_lecture():
    """已关联本讲 requires an explicit link to the SELECTED lecture; a link to
    another lecture is course-level from this lecture's perspective."""
    js = assets(make_client())["js"]
    label_fn = js_function(js, "associationLabel")
    assert "item.linked_to_lecture" in label_fn
    assert "state.lectureId" in label_fn
    assert "item.lecture.id === state.lectureId" in label_fn
    assert 'association_state === "待确认"' in label_fn
    assert "copy.course_level" in label_fn
    row_fn = js_function(js, "materialRow")
    assert "associationLabel(item)" in row_fn
    # every row keeps the identity-only 在 Notion 查看 action
    assert '"data-kind": "material"' in row_fn
    assert "copy.open" in row_fn


def test_renderer_emits_the_pending_badge_for_both_pending_semantics():
    """The two 待确认 semantics render with distinct, assertable classes:
    the association badge under the title and the 处理状态 pill."""
    js = assets(make_client())["js"]
    row_fn = js_function(js, "materialRow")
    assert 'class="assoc ' in row_fn  # association label carries a kind class
    assert 'data-assoc="' in row_fn
    assert 'class="status-badge ' in row_fn  # the 处理状态 pill keeps its own
    assert '"indexed" : "pending"' in row_fn  # 已索引 green, others pending


def test_renderer_wraps_the_inferred_subsection_visually_apart():
    js = assets(make_client())["js"]
    body = js_function(js, "materialsBody")
    assert "pending-subsection" in body  # a visually separated subsection
    assert 'data-state="pending"' in body
    assert "copy.pending_group" in body
    group_fn = js_function(js, "groupLabel")
    assert 'data-group="' in group_fn
    assert '"pending"' in group_fn  # the subsection label is pending-styled


def test_view_switching_re_renders_in_place_without_reload():
    js = assets(make_client())["js"]
    switch_fn = js_function(js, "setMaterialView")
    assert "lectureId" not in switch_fn  # switching NEVER changes the lecture
    assert "render()" in switch_fn and "loadMaterials()" in switch_fn
    # in-place re-render only: no full-page reload anywhere in the renderer
    # (the "reload-directory" action name is the sync button, not navigation)
    assert "location.reload" not in js
    assert ".reload()" not in js


def test_pending_styles_use_the_pending_family_never_the_indexed_green():
    css = assets(make_client())["css"]
    assoc = css_block(css, ".assoc.pending")
    assert "var(--blue)" in assoc and "var(--blue-pale)" in assoc
    assert "var(--green)" not in assoc  # never the indexed green style
    status = css_block(css, ".status-badge.pending")
    assert "var(--blue)" in status and "var(--blue-pale)" in status
    assert "var(--green)" not in status
    subsection = css_block(css, ".pending-subsection")
    assert "var(--green)" not in subsection
    # the two semantics stay structurally distinguishable: distinct selectors
    assert ".assoc.pending" != ".status-badge.pending"
    assert subsection.startswith(".pending-subsection")
