"""Unit tests for the TUI's pure data layer (no textual needed)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from pku_sync import tui_data
from pku_sync.models import Assignment, Course, Recording


def _write_course(root: Path, folder: str, *, md: str = "", deadlines: list[dict] | None = None) -> None:
    course = root / folder
    course.mkdir(parents=True)
    (course / "course.json").write_text('{"name": "示例课"}', encoding="utf-8")
    if md:
        (course / "assignments.md").write_text(md, encoding="utf-8")
    (course / "deadlines.json").write_text(
        __import__("json").dumps(deadlines or []), encoding="utf-8"
    )


def test_parse_daily_log_extracts_stamp_and_exit_code():
    status = tui_data.parse_daily_log(
        "=== pku-sync daily started 20260916_060002 ===\nsome output\n=== EXIT_CODE=0 ===\n"
    )
    assert status.started == "20260916_060002"
    assert status.exit_code == 0
    assert status.ok


def test_parse_daily_log_failure_and_missing_markers():
    failed = tui_data.parse_daily_log("=== pku-sync daily started 20260916_060002 ===\n=== EXIT_CODE=3 ===\n")
    assert not failed.ok
    assert failed.exit_code == 3
    empty = tui_data.parse_daily_log("truncated log, no markers")
    assert empty.started is None
    assert empty.exit_code is None
    assert not empty.ok


def test_parse_due_formats():
    assert tui_data.parse_due("2026-09-13T15:59:00.000Z") is not None
    assert tui_data.parse_due("2026-09-13T15:59:00.0000000Z") is not None  # .NET 7 digits
    assert tui_data.parse_due("2026-09-13 23:59") == datetime(2026, 9, 13, 23, 59)
    assert tui_data.parse_due("2026-09-13") == datetime(2026, 9, 13)
    assert tui_data.parse_due("未公布") is None
    assert tui_data.parse_due("") is None


def test_collect_deadlines_merges_md_json_and_keeps_undated(tmp_path):
    _write_course(
        tmp_path,
        "示例课_26-27学年第1学期",
        md=(
            "# 作业与截止时间\n\n"
            "| 截止时间 | 作业 | 来源 |\n| --- | --- | --- |\n"
            "| 未公布 | 作业1 四象限 | content-tree |\n"
            "| 2026-09-20 12:00 | 作业2 | calendar |\n"
        ),
        deadlines=[{"title": "作业2", "due_at": "2026-09-20T04:00:00.000Z", "source": "calendar"}],
    )
    deadlines = tui_data.collect_deadlines(tmp_path)
    titles = [d.title for d in deadlines]
    assert titles == ["作业2", "作业1 四象限"]  # dated first, undated kept
    assert deadlines[0].course == "示例课"  # course.json name wins over folder
    assert deadlines[1].due is None


def test_upcoming_window_includes_short_overdue_and_excludes_far(tmp_path):
    today = datetime(2026, 9, 16)
    items = [
        tui_data.Deadline("课", "过期3天", "2026-09-13", due=datetime(2026, 9, 13)),
        tui_data.Deadline("课", "今天", "2026-09-16", due=datetime(2026, 9, 16)),
        tui_data.Deadline("课", "第10天", "2026-09-26", due=datetime(2026, 9, 26)),
        tui_data.Deadline("课", "太远", "2026-12-01", due=datetime(2026, 12, 1)),
        tui_data.Deadline("课", "过期30天", "2026-08-17", due=datetime(2026, 8, 17)),
        tui_data.Deadline("课", "未公布", "未公布", due=None),
    ]
    window = tui_data.upcoming(items, today=today, days=14)
    assert [item.title for item, _ in window] == ["过期3天", "今天", "第10天"]
    assert [left for _, left in window] == [-3, 0, 10]


def test_latest_report_report_date_and_freshness(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "review_20260915.md").write_text("a", encoding="utf-8")
    (logs / "review_20260916.md").write_text("b", encoding="utf-8")
    assert tui_data.latest_report(logs, "review") == "review_20260916.md"
    assert tui_data.latest_report(logs, "lecture") is None
    assert tui_data.report_date("review_20260916.md") == "2026-09-16"
    assert tui_data.report_date(None) is None
    assert tui_data.freshness("2026-09-16", datetime(2026, 9, 16)) == "今天"
    assert tui_data.freshness("2026-09-14", datetime(2026, 9, 16)) == "2 天前"
    assert tui_data.freshness(None) == "无记录"


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

    snapshot = tui_data.fetch_live_snapshot(
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
        ("认知心理学", 1, 1),
    ]
    assert snapshot.courses[1].nearest_due == "2026-09-20 12:00+08:00"
    assert [(row.course, row.title, row.due_at) for row in snapshot.deadlines] == [
        ("认知心理学", "作业一", "2026-09-20T12:00:00+08:00"),
        ("计算机网络", "作业一", "未公布"),
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

    snapshot = tui_data.fetch_live_snapshot(
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

    snapshot = tui_data.fetch_live_snapshot(
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
        return tui_data.fetch_live_snapshot(
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
        tui_data.fetch_live_snapshot(SimpleNamespace(), session_factory=fail_login)
    except RuntimeError as exc:
        assert str(exc) == "IAAA login failed"
    else:
        raise AssertionError("authentication failure should be visible")


def test_fetch_live_snapshot_reports_monotonic_progress():
    client = SimpleNamespace(close=lambda: None)
    courses = [Course(course_id=f"_{i}_1", name=f"课{i}") for i in (1, 2, 3)]
    seen: list[tuple[int, int, str]] = []

    snapshot = tui_data.fetch_live_snapshot(
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

    snapshot = tui_data.fetch_live_snapshot(
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


def test_collect_local_workbench_reports_materials_recordings_and_notion(tmp_path):
    course = tmp_path / "示例课"
    (course / "materials" / "第一讲").mkdir(parents=True)
    (course / "announcements").mkdir()
    (course / "recordings" / "2026-09-01_第一讲" / "keyframes").mkdir(parents=True)
    (course / "course.json").write_text(
        '{"course_id":"_1_1","name":"示例课"}', encoding="utf-8"
    )
    (course / "materials" / "第一讲" / "_index.md").write_text("# 讲义", "utf-8")
    (course / "announcements" / "通知.md").write_text("# 通知", "utf-8")
    (course / "deadlines.json").write_text(
        '[{"title":"作业一","due_at":"2026-09-20"}]', encoding="utf-8"
    )
    (course / "recordings" / "index.json").write_text(
        '[{"course_id":"_1_1","title":"第一讲","recorded_at":"2026-09-01 08:00:00"}]',
        encoding="utf-8",
    )
    recording_dir = course / "recordings" / "2026-09-01_第一讲"
    (recording_dir / "notes.md").write_text("笔记" * 10, "utf-8")
    (recording_dir / "keyframes" / "frame_0001_00m01s.jpg").write_bytes(b"jpg")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "review_20260916.md").write_text("晨检完成", "utf-8")

    snapshot = tui_data.collect_local_workbench(tmp_path)

    assert snapshot.courses[0].material_files == ("materials/第一讲/_index.md",)
    assert snapshot.courses[0].announcement_files == ("announcements/通知.md",)
    assert snapshot.courses[0].assignment_count == 1
    assert snapshot.courses[0].recordings_ready == 1
    assert snapshot.courses[0].recordings[0].keyframes == 1
    assert snapshot.notion.review_report == "review_20260916.md"
    assert "晨检完成" in snapshot.notion.review_excerpt
