"""Tests for read-only course data and assignment dates."""


from __future__ import annotations


from datetime import datetime




from types import SimpleNamespace


from pku_sync import course_data


from pku_sync.models import Assignment, Course, Recording


def test_parse_due_formats():
    assert course_data.parse_due("2026-09-13T15:59:00.000Z") is not None
    assert course_data.parse_due("2026-09-13T15:59:00.0000000Z") is not None  # .NET 7 digits
    assert course_data.parse_due("2026-09-13 23:59") == datetime(2026, 9, 13, 23, 59)
    assert course_data.parse_due("2026-09-13") == datetime(2026, 9, 13)
    assert course_data.parse_due("未公布") is None
    assert course_data.parse_due("") is None


def test_fetch_live_snapshot_reads_each_selected_course_and_closes_client():
    client = SimpleNamespace(closed=False)
    client.close = lambda: setattr(client, "closed", True)
    courses = [
        Course(course_id="_2_1", name="计算机网络"),
        Course(course_id="_1_1", name="认知心理学"),
    ]
    calendar = {
        "_1_1": [
            Assignment(
                course_id="_1_1",
                title="作业一",
                due_at="2026-09-20T12:00:00+08:00",
                source="calendar",
            )
        ]
    }

    def materials(_client, course_id):
        rows = [Assignment(course_id=course_id, title="作业一", source="content-tree")]
        return [], rows

    snapshot = course_data.fetch_live_snapshot(
        SimpleNamespace(),
        session_factory=lambda: client,
        discover_fn=lambda _client: courses,
        select_fn=lambda found, _config: found,
        deadlines_fn=lambda _client: calendar,
        materials_fn=materials,
        recordings_fn=lambda _client, course_id: [
            Recording(course_id=course_id, title="第一讲")
        ],
        now_fn=lambda: datetime(2026, 9, 16, 9, 30),
    )

    assert client.closed
    assert snapshot.fetched_at == datetime(2026, 9, 16, 9, 30)
    assert [(row.course, row.assignments, row.recordings) for row in snapshot.courses] == [
        ("计算机网络", 1, 1),
        # No native content identity: calendar fact stays separate from the task.
        ("认知心理学", 2, 1),
    ]
    assert snapshot.courses[1].nearest_due == "2026-09-20 12:00+08:00"
    assert [(row.course, row.title, row.due_at) for row in snapshot.deadlines] == [
        ("认知心理学", "作业一", "2026-09-20T12:00:00+08:00"),
        ("计算机网络", "作业一", "未公布"),
        ("认知心理学", "作业一", "未公布"),
    ]


def test_fetch_live_snapshot_isolates_course_errors_and_global_calendar_warning():
    client = SimpleNamespace(close=lambda: None)
    courses = [Course(course_id="_1_1", name="坏课"), Course(course_id="_2_1", name="好课")]

    def materials(_client, course_id):
        if course_id == "_1_1":
            raise RuntimeError("内容树拒绝访问")
        return [], [Assignment(course_id=course_id, title="无日期作业")]

    def recordings(_client, course_id):
        if course_id == "_1_1":
            raise RuntimeError("录播未开通")
        return []

    snapshot = course_data.fetch_live_snapshot(
        SimpleNamespace(),
        session_factory=lambda: client,
        discover_fn=lambda _client: courses,
        select_fn=lambda found, _config: found,
        deadlines_fn=lambda _client: (_ for _ in ()).throw(RuntimeError("日历 503")),
        materials_fn=materials,
        recordings_fn=recordings,
    )

    assert snapshot.warnings == ("全局截止时间读取失败：日历 503",)
    bad, good = snapshot.courses
    assert bad.course == "坏课"
    assert bad.errors == ("作业：内容树拒绝访问", "录音：录播未开通")
    assert good.course == "好课" and not good.errors
    assert snapshot.deadlines[0].title == "无日期作业"


def test_fetch_live_snapshot_resolves_placeholder_names_via_shared_discovery():
    client = SimpleNamespace(close=lambda: None)
    discovered = [Course(course_id="_104128_1", name="_104128_1")]

    def materials(_client, course_id):
        return [], [Assignment(course_id=course_id, title="作业一")]

    def recordings(_client, course_id):
        return [Recording(course_id=course_id, title="第一讲")]

    snapshot = course_data.fetch_live_snapshot(
        SimpleNamespace(),
        session_factory=lambda: client,
        discover_fn=lambda _client: discovered,
        resolve_name_fn=lambda _client, course_id: {"_104128_1": "计算机网络"}[course_id],
        select_fn=lambda found, _config: found,
        deadlines_fn=lambda _client: {},
        materials_fn=materials,
        recordings_fn=recordings,
    )

    assert snapshot.courses[0].course == "计算机网络"
    assert snapshot.courses[0].course_id == "_104128_1"


def test_fetch_live_snapshot_empty_discovery_and_selection_errors():
    client = SimpleNamespace(close=lambda: None)

    def run(discovered, select):
        return course_data.fetch_live_snapshot(
            SimpleNamespace(),
            session_factory=lambda: client,
            discover_fn=lambda _client: discovered,
            select_fn=select,
        )

    try:
        run([], lambda found, _config: found)
    except RuntimeError as exc:
        assert str(exc) == "教学网没有返回任何课程，请检查登录状态或学期筛选"
    else:
        raise AssertionError("empty discovery should raise")

    course = Course(course_id="_1_1", name="认知心理学")
    try:
        run([course], lambda found, _config: [])
    except RuntimeError as exc:
        assert str(exc) == "当前学期或课程筛选没有选中任何教学网课程"
    else:
        raise AssertionError("empty selection should raise")


def test_fetch_live_snapshot_surfaces_auth_failure_without_local_fallback():
    def fail_login():
        raise RuntimeError("IAAA login failed")

    try:
        course_data.fetch_live_snapshot(SimpleNamespace(), session_factory=fail_login)
    except RuntimeError as exc:
        assert str(exc) == "IAAA login failed"
    else:
        raise AssertionError("authentication failure should be visible")


def test_fetch_live_snapshot_reports_monotonic_progress():
    client = SimpleNamespace(close=lambda: None)
    courses = [Course(course_id=f"_{i}_1", name=f"课{i}") for i in (1, 2, 3)]
    seen: list[tuple[int, int, str]] = []

    snapshot = course_data.fetch_live_snapshot(
        SimpleNamespace(),
        session_factory=lambda: client,
        discover_fn=lambda _client: courses,
        select_fn=lambda found, _config: found,
        deadlines_fn=lambda _client: {},
        materials_fn=lambda _client, course_id: ([], []),
        recordings_fn=lambda _client, course_id: [],
        progress_fn=lambda done, total, name: seen.append((done, total, name)),
        max_workers=1,
    )

    assert [done for done, _, _ in seen] == [1, 2, 3]  # 逐课推进，单调不减
    assert all(total == 3 for _, total, _ in seen)
    assert seen[-1][2] == "课3"
    assert len(snapshot.courses) == 3


def test_fetch_live_snapshot_fetches_courses_concurrently():
    import threading

    client = SimpleNamespace(close=lambda: None)
    courses = [Course(course_id=f"_{i}_1", name=f"课{i}") for i in (1, 2)]
    barrier = threading.Barrier(2, timeout=5)

    def materials(_client, course_id):
        barrier.wait()  # 两课同时在途才会双双到达；退化为串行则屏障破裂
        return [], []

    snapshot = course_data.fetch_live_snapshot(
        SimpleNamespace(),
        session_factory=lambda: client,
        discover_fn=lambda _client: courses,
        select_fn=lambda found, _config: found,
        deadlines_fn=lambda _client: {},
        materials_fn=materials,
        recordings_fn=lambda _client, course_id: [],
        max_workers=2,
    )

    assert len(snapshot.courses) == 2
    assert not any(course.errors for course in snapshot.courses)  # 屏障未破裂
