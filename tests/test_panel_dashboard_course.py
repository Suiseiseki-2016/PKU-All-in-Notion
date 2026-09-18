"""Dashboard / course-detail / lecture-detail panel surfaces (VAL-META-021..024).

The three screens are rendered in the browser from ``window.PANEL_COPY`` plus
the directory API, so these tests pin the two halves that make the browser
assertions meaningful:

- the API side: the dashboard's numbers (stats, course counts, the recent
  activity feed, the last-sync timestamp) all come from the seeded adapter
  read — never from a hardcoded demo constant — and cross-add to each other,
  which is exactly the DOM-vs-payload cross-check VAL-META-021 asks for;
- the copy side: every string the three screens render is pinned in
  ``PANEL_COPY`` against the approved prototype (docs/UI_DESIGN.html), and the
  shipped assets carry none of the prototype's demo numbers, no lecture body
  probe, and no forbidden path/token fragment.
"""

from __future__ import annotations

import datetime
import json
import re

from fastapi.testclient import TestClient

from pku_sync.panel.directory import (
    ACTIVITY_LIMIT,
    build_activity,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import (
    BODY_PROBE,
    COG_L1,
    COURSE_COG,
    COURSE_CS,
    COURSE_DEV,
    COURSE_NET,
    DEV_L1,
    DEV_ROWS,
    NET_L1,
    NET_L2,
    NET_ROWS,
    PANEL_FORBIDDEN_STRINGS,
)
from pku_sync.panel.student_ui import (
    COURSE_COPY,
    DASHBOARD_COPY,
    LECTURE_COPY,
    PANEL_COPY,
    STUDENT_UI_PATH,
)
from pku_sync.panel.webapi import create_app

FIXED_NOW = datetime.datetime(
    2026, 9, 19, 9, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
)

ACTIVITY_KEYS = {"id", "url", "title", "course", "type", "status", "updated"}


def make_client() -> TestClient:
    service = build_fake_directory(clock=lambda: FIXED_NOW)
    return TestClient(create_app(directory_service=service))


def directory(client: TestClient) -> dict:
    response = client.get("/api/directory")
    assert response.status_code == 200
    return response.json()


def course_of(payload: dict, course_id: str) -> dict:
    return next(course for course in payload["courses"] if course["id"] == course_id)


def lecture_of(payload: dict, course_id: str, lecture_id: str) -> dict:
    course = course_of(payload, course_id)
    return next(lecture for lecture in course["lectures"] if lecture["id"] == lecture_id)


def assets(client: TestClient) -> dict[str, str]:
    page = client.get(STUDENT_UI_PATH)
    css = client.get(f"{STUDENT_UI_PATH}/app.css")
    script = client.get(f"{STUDENT_UI_PATH}/app.js")
    for response in (page, css, script):
        assert response.status_code == 200
    return {"page": page.text, "css": css.text, "js": script.text}


# -- VAL-META-021: every dashboard number comes from the adapter ----------------


def test_dashboard_stats_cross_add_with_the_course_cards():
    payload = directory(make_client())
    stats = payload["stats"]
    # the three stat cards and the course cards read the SAME adapter counts,
    # so a DOM cross-check can add the cards up to the stat values
    assert stats["lectures"] == sum(c["counts"]["lectures"] for c in payload["courses"])
    assert stats["materials"] == sum(c["counts"]["materials"] for c in payload["courses"])
    assert stats["courses"] == len(payload["courses"]) == 4
    assert stats["lectures"] == 4
    assert stats["materials"] == 10
    # the 上次同步 card and the sync pill read the sync snapshot's timestamp
    assert payload["sync"]["last_sync_at"] == FIXED_NOW.isoformat(timespec="seconds")
    assert payload["sync"]["state"] == "done"


def test_recent_activity_is_built_from_real_index_rows_newest_first():
    payload = directory(make_client())
    activity = payload["activity"]
    assert len(activity) == ACTIVITY_LIMIT == 3
    assert [entry["title"] for entry in activity] == [
        "第三讲发展心理学讲义.pdf",
        "第二讲 应用层讲义.pdf",
        "第一讲（1）课程介绍.pdf",
    ]
    assert [entry["course"] for entry in activity] == ["发展心理学", "计算机网络", "计算机网络"]
    assert [entry["id"] for entry in activity] == [DEV_ROWS[0], NET_ROWS[2], NET_ROWS[0]]
    stamps = [entry["updated"] for entry in activity]
    assert stamps == sorted(stamps, reverse=True)
    for entry in activity:
        assert set(entry) == ACTIVITY_KEYS
        assert entry["url"].startswith("https://www.notion.so/")
        assert entry["type"]  # a real 资料类型 label, never an invented one


def test_activity_entries_keep_the_material_allowlist_and_no_honeypots():
    payload = directory(make_client())
    blob = json.dumps(payload, ensure_ascii=False)
    for needle in PANEL_FORBIDDEN_STRINGS:
        assert needle not in blob, needle
    assert "来源路径" not in blob and "备注" not in blob


def test_activity_is_empty_when_no_indexed_row_carries_a_timestamp():
    service = build_fake_directory(clock=lambda: FIXED_NOW)
    data = service.provider.load()
    for item in data.materials:
        item.updated = ""
    assert build_activity(data) == []


def test_dashboard_copy_matches_the_approved_prototype():
    assert DASHBOARD_COPY["overview"] == {"title": "学习空间概览", "note": "索引与同步状态"}
    stats = DASHBOARD_COPY["stats"]
    assert stats["lectures"]["label"] == "已索引讲次"
    assert stats["lectures"]["unit"] == "个"
    assert stats["materials"]["label"] == "已索引资料"
    assert stats["materials"]["unit"] == "份"
    assert stats["materials"]["foot"] == "课件 · 手册 · 课堂录像笔记"
    assert stats["sync"]["label"] == "上次同步"
    assert stats["sync"]["foot"] == "索引与状态保持最新"
    assert DASHBOARD_COPY["activity"]["title"] == "最近动态"
    assert DASHBOARD_COPY["activity"]["note"] == "索引、同步与 Notion 学习记录"
    assert DASHBOARD_COPY["course_action"] == "查看课程"
    assert DASHBOARD_COPY["sync_action"] == "同步课程"
    assert DASHBOARD_COPY["sync_pill"] == "上次同步 · {relative}"
    # the counts line is one template with the three adapter-derived slots
    assert PANEL_COPY["directory"]["course_counts"] == (
        "{lectures} 个讲次 · {materials} 份课程资料 · 最近同步 {synced}"
    )


def test_ui_assets_carry_no_prototype_demo_numbers():
    text = assets(make_client())["js"]
    for demo in ("12 分钟前", "8 个讲次", "12 份课程资料", "21", "CS201", "林同学"):
        assert demo not in text, demo
    # the stat values are read out of the payload, not written into the markup
    assert "stats.lectures" in text and "stats.materials" in text


# -- VAL-META-022: course detail hero, lecture hints, materials overview --------


def test_course_copy_matches_the_approved_prototype():
    assert COURSE_COPY["kicker"] == "{scope}学习中心 · Notion 课程页"
    assert COURSE_COPY["desc"] == (
        "课程页包含讲次索引、资料概览与同步状态。选择一个讲次，查看标题、日期与关联资料清单；"
        "正文、笔记与练习都在 Notion 讲次页中打开。"
    )
    assert COURSE_COPY["sync_action"] == "同步已选内容"
    lectures = COURSE_COPY["lectures"]
    assert lectures["title"] == "课程讲次"
    assert lectures["note"] == "{lectures} 个讲次 · 选择后查看讲次索引"
    assert lectures["hint_mapped"] == "{topic} · {count} 份关联资料"
    assert lectures["hint_unmapped"] == "资料映射待确认 · 可先查看课程资料"
    materials = COURSE_COPY["materials"]
    assert materials["title"] == "课程资料概览"
    assert materials["note"] == "课程级"
    assert materials["total_unit"] == "份已索引资料"
    assert materials["row_unit"] == "{count} 份"
    assert materials["source_note"] == (
        "来源：课程资料索引 · 课堂录像笔记\n"
        "只显示你选中并已索引的内容；客户端不保存资料正文。"
    )


def test_seeded_fixture_exercises_both_lecture_hint_variants():
    payload = directory(make_client())
    mapped = lecture_of(payload, COURSE_NET, NET_L1)
    unmapped = lecture_of(payload, COURSE_NET, NET_L2)
    # mapped → '<topic> · N 份关联资料' with N from the adapter's confirmed links
    assert mapped["mapping_state"] == "mapped"
    assert mapped["counts"]["related_materials"] == 2
    # unmapped (inferred-only) → the mapping-unconfirmed hint
    assert unmapped["mapping_state"] == "unmapped"
    assert unmapped["counts"]["related_materials"] == 0
    assert unmapped["counts"]["inferred_materials"] >= 1
    # a second unmapped course and an entirely material-free lecture exist too
    assert lecture_of(payload, COURSE_DEV, DEV_L1)["mapping_state"] == "unmapped"
    cog = lecture_of(payload, COURSE_COG, COG_L1)
    assert cog["counts"] == {
        "confirmed_materials": 0,
        "inferred_materials": 0,
        "related_materials": 0,
    }


def test_course_material_overview_rows_add_up_to_the_course_pool():
    client = make_client()
    payload = directory(client)
    for course_id in (COURSE_NET, COURSE_DEV, COURSE_COG, COURSE_CS):
        course = course_of(payload, course_id)
        groups = client.get(
            f"/api/courses/{course_id}/materials", params={"view": "type"}
        ).json()["groups"]
        # the overview's per-type rows ARE the type view's groups, so the rows
        # sum to the course card's 份课程资料 number
        assert sum(group["count"] for group in groups) == course["counts"]["materials"]
    net_groups = client.get(
        f"/api/courses/{COURSE_NET}/materials", params={"view": "type"}
    ).json()["groups"]
    labels = [group["type"] for group in net_groups]
    assert "课堂课件" in labels and "课堂笔记" in labels and "课程手册" in labels


# -- VAL-META-023/024: lecture detail copy + the view reset --------------------


def test_lecture_copy_matches_the_approved_prototype():
    assert LECTURE_COPY["open_lecture"] == "在 Notion 打开讲次页"
    assert LECTURE_COPY["desc"] == (
        "{course} · {scope}\n"
        "本页是讲次索引：标题、日期、同步状态与关联资料。完整内容在 Notion 讲次页中打开。"
    )
    info = LECTURE_COPY["info"]
    assert info["title"] == "讲次信息"
    assert [info["date"], info["duration"], info["sync"], info["related"], info["page"]] == [
        "上课日期",
        "录制时长",
        "同步状态",
        "关联资料",
        "Notion 页面",
    ]
    assert info["related_value"] == "{count} 份"
    assert info["related_pending_suffix"] == " · 映射待确认"
    assert info["page_ready"] == "讲次页已就绪"
    assert info["page_pending_suffix"] == " · 资料关联待确认"
    assert info["source_note"] == (
        "客户端只保存讲次与资料的索引信息，不保存课堂正文、转写或资料内容。"
    )
    launch = LECTURE_COPY["launch"]
    assert launch["title"] == "在 Notion 继续"
    assert launch["notes"]["title"] == "写笔记"
    assert launch["notes"]["detail"] == "在讲次页的笔记区记录想法"
    assert launch["notes"]["action"] == "在 Notion 写笔记"
    assert launch["quiz"]["title"] == "做练习"
    assert launch["quiz"]["detail"] == "打开本讲关联的练习页"
    assert launch["quiz"]["action"] == "在 Notion 开始练习"
    views = LECTURE_COPY["materials"]["views"]
    assert views == {"lecture": "按讲次", "type": "按资料类型", "all": "全部资料"}


def test_lecture_selection_resets_the_material_view_in_the_renderer():
    text = assets(make_client())["js"]
    # the reset is a property of selection itself: every lecture selection
    # (including re-selection after a view switch) lands on 按讲次
    match = re.search(r"function selectLecture\([\s\S]{0,600}?\n  \}", text)
    assert match, "selectLecture must exist"
    assert 'state.materialView = "lecture"' in match.group(0)
    assert 'aria-pressed="' in text


def test_lecture_body_probe_never_reaches_the_panel():
    client = make_client()
    payload = directory(client)
    blobs = [json.dumps(payload, ensure_ascii=False)]
    for view in ("type", "all"):
        blobs.append(
            json.dumps(
                client.get(
                    f"/api/courses/{COURSE_NET}/materials", params={"view": view}
                ).json(),
                ensure_ascii=False,
            )
        )
    blobs.append(
        json.dumps(
            client.get(
                f"/api/courses/{COURSE_NET}/materials",
                params={"view": "lecture", "lecture_id": NET_L1},
            ).json(),
            ensure_ascii=False,
        )
    )
    blobs.extend(assets(client).values())
    for blob in blobs:
        assert BODY_PROBE not in blob
        assert "课堂正文标记" not in blob
