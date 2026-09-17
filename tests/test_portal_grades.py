"""Unit tests for the portal course table and gradebook read-only clients."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pku_sync import grades, portal
from pku_sync.grades import GradesError
from pku_sync.portal import (
    PortalError,
    course_name,
    format_course_info,
    parse_course_table,
    render_course_table,
    table_unavailable,
)

# Verbatim cells from a live 25-26-2 course table: the portal wraps names in
# markup, marks the enrolment kind, and packs every detail block into one string.
DUAL_DEGREE_CELL = (
    "实验心理学(辅双)<br>上课信息：1-15周 每周 三教407  教师：耿海燕,张俊云 "
    "备注：先修普心或同类课程、心统1<br>考试信息：20260616 星期二 上午 二教423"
)
HTML_WRAPPED_CELL = (
    "<font color = 'red'><b>计算机视觉导论(主)<br>上课信息：9-9周 每周 二教101 "
    "教师：王鹤<br>考试信息：20260624 星期三 下午 二教105"
)

COURSE_TABLE_BODY = {
    "course": [
        {"mon": {"courseName": "认知心理学(主)上课信息：文史楼113 教师：张三 考试信息：随堂"},
         "wed": {"courseName": "计算机网络 上课信息：二教319 教师：李四"}, "fri": None},
        {"mon": {"courseName": "认知心理学(主)上课信息：文史楼113 教师：张三 考试信息：随堂"},
         "wed": None, "fri": None},
        {"mon": None, "wed": {"courseName": "计算机网络 上课信息：二教319 教师：李四"}, "fri": None},
        {},
    ]
}


def test_format_course_info_strips_markers():
    info = "认知心理学(主)上课信息：文史楼113 教师：张三 考试信息：随堂"
    out = format_course_info(info)
    assert "认知心理学" in out and "(主)" not in out
    assert "上课：文史楼113" in out
    assert "教师：张三" in out
    assert "考试：随堂" in out


def test_format_course_info_keeps_dual_degree_marker_and_splits_remark():
    assert format_course_info(DUAL_DEGREE_CELL) == (
        "实验心理学(辅双) ｜ 上课：1-15周 每周 三教407 ｜ 教师：耿海燕,张俊云"
        " ｜ 备注：先修普心或同类课程、心统1 ｜ 考试：20260616 星期二 上午 二教423"
    )


def test_format_course_info_drops_html_wrapper():
    assert format_course_info(HTML_WRAPPED_CELL) == (
        "计算机视觉导论 ｜ 上课：9-9周 每周 二教101 ｜ 教师：王鹤"
        " ｜ 考试：20260624 星期三 下午 二教105"
    )


@pytest.mark.parametrize("cell", [DUAL_DEGREE_CELL, HTML_WRAPPED_CELL])
def test_format_course_info_leaks_no_markup_or_block_labels(cell):
    out = format_course_info(cell)
    for leak in ("<", ">", "上课信息：", "考试信息："):
        assert leak not in out


def test_course_name_only_drops_primary_major_marker():
    assert course_name(HTML_WRAPPED_CELL) == "计算机视觉导论"
    assert course_name(DUAL_DEGREE_CELL) == "实验心理学(辅双)"


def test_parse_course_table_groups_consecutive_slots():
    slots = parse_course_table(COURSE_TABLE_BODY)
    by_day = {(s.day, s.start_slot, s.end_slot): s.info for s in slots}
    # 周一 1-2 节连续（同一课程同一教师）。
    assert (("周一", 1, 2),) and any(s.day == "周一" and s.start_slot == 1 and s.end_slot == 2 for s in slots)
    # 周三第1节和第3节各一条（第2节无该课，不合并）。
    wed = [s for s in slots if s.day == "周三"]
    assert wed == [
        portal.CourseSlot("周三", 1, 1, wed[0].info),
        portal.CourseSlot("周三", 3, 3, wed[1].info),
    ]


def test_render_course_table_human_lines():
    lines = render_course_table(COURSE_TABLE_BODY)
    assert lines[0] == "个人课表"
    assert any("周一" in line and "第1-2节" in line for line in lines)
    assert any("周三" in line and "第3节" in line for line in lines)


def test_render_course_table_empty():
    assert render_course_table({"course": []}) == ["暂无课表数据"]


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if url not in self.routes:
            raise AssertionError(f"unexpected url {url}")
        return SimpleNamespace(status_code=200, json=lambda: self.routes[url],
                               raise_for_status=lambda: None)


TERM_LIST_BODY = {
    "success": True,
    "nowXnxq": {"xndxqValue": "26-27学年1学期", "xndxq": "26-27-1"},
    "xndxq": [
        {"xndxqValue": "26-27学年1学期", "xndxq": "26-27-1"},
        {"xndxqValue": "25-26学年2学期", "xndxq": "25-26-2"},
    ],
}


def test_fetch_terms_reads_current_and_selectable():
    client = FakeHttp({portal.XNDXQ_LIST: TERM_LIST_BODY})
    current, terms = portal.fetch_terms(client)
    assert current == "26-27-1"
    assert [t.code for t in terms] == ["26-27-1", "25-26-2"]
    assert terms[1].label == "25-26学年2学期"


def test_fetch_course_table_defaults_to_current_term():
    client = FakeHttp({portal.XNDXQ_LIST: TERM_LIST_BODY, portal.COURSE_INFO: COURSE_TABLE_BODY})
    body, used = portal.fetch_course_table(client)
    assert used == "26-27-1"
    assert body is COURSE_TABLE_BODY
    assert (portal.COURSE_INFO, {"xndxq": "26-27-1"}) in client.calls


def test_fetch_course_table_with_explicit_term_skips_term_lookup():
    client = FakeHttp({portal.COURSE_INFO: COURSE_TABLE_BODY})
    _body, used = portal.fetch_course_table(client, "25-26-2")
    assert used == "25-26-2"
    # No getXndXqList round trip when the caller already named the term.
    assert [url for url, _ in client.calls] == [portal.COURSE_INFO]


def test_table_unavailable_reports_portal_message():
    # The portal answers success=true with no "course" key for an unpublished term.
    assert table_unavailable({"success": True, "message": "获取个人课表信息失败"}) == (
        "获取个人课表信息失败"
    )
    assert table_unavailable({"course": []}) == ""


def test_gradebook_endpoints_and_calculated_filter():
    routes = {
        # /users/me answers with the user object itself, not a paged envelope.
        grades.USERS_ME: {"id": "u1", "userName": "2300012922"},
        # Enrollments carry ids only; the name needs a separate course lookup.
        grades.USER_COURSES.format(user_id="u1"): {
            "results": [
                {"courseId": "c1", "courseRoleId": "Student"},
                {"courseId": "c2", "courseRoleId": "TeachingAssistant"},
            ]
        },
        grades.COURSE_DETAIL.format(course_id="c1"): {"id": "c1", "name": "认知心理学"},
        # Learn reports the column kind as grading.type.
        grades.GRADEBOOK_COLUMNS.format(course_id="c1"): {
            "results": [
                {"id": "col1", "name": "期中", "grading": {"type": "Manual"},
                 "score": {"possible": 40.0}},
                {"id": "total", "name": "成绩总计", "grading": {"type": "Calculated"},
                 "score": {"possible": 100.0}},
                {"id": "usual", "name": "平时总分总计", "grading": {"type": "Calculated"},
                 "score": {"possible": 20.0}},
            ]
        },
        grades.GRADEBOOK_USERS.format(course_id="c1", column_id="col1"): {
            "results": [{"displayGrade": {"score": 35.5}}]
        },
        grades.GRADEBOOK_USERS.format(course_id="c1", column_id="usual"): {
            "results": [{"displayGrade": {"score": 18}}]
        },
    }
    client = FakeHttp(routes)
    records = grades.fetch_all_grades(client)
    # Helper course excluded; Calculated 成绩总计 excluded; 平时总分总计 kept.
    names = [r.column_name for r in records]
    assert names == ["期中", "平时总分总计"]
    mid = records[0]
    assert mid.course_name == "认知心理学"
    assert mid.score == 35.5 and mid.possible == 40.0


def test_course_title_falls_back_to_id_when_lookup_fails():
    client = FakeHttp({grades.COURSE_DETAIL.format(course_id="c9"): {"id": "c9"}})
    assert grades.course_title(client, "c9") == "c9"


def test_grading_type_reads_type_with_legacy_fallback():
    assert grades._grading_type({"grading": {"type": "Calculated"}}) == "Calculated"
    assert grades._grading_type({"grading": {"gradingType": "Manual"}}) == "Manual"
    assert grades._grading_type({}) == ""


def test_current_user_id_reads_unwrapped_object():
    client = FakeHttp({grades.USERS_ME: {"id": "_170580_1", "userName": "2300012922"}})
    assert grades.current_user_id(client) == "_170580_1"


@pytest.mark.parametrize("body", [{}, {"results": [{"id": "u1"}]}])
def test_grades_error_when_no_user_id(body):
    # A paged envelope carries no top-level id, so it must fail loudly.
    client = FakeHttp({grades.USERS_ME: body})
    with pytest.raises(GradesError):
        grades.student_courses(client)
