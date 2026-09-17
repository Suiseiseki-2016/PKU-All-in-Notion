"""Smoke tests for the textual App (pure construction + data builders).

Kept synchronous on purpose: constructing PkuSyncApp already parses the CSS
and validates the bindings, and the panel builders only touch the filesystem,
so no event loop is needed for meaningful coverage.
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("textual", reason="TUI smoke test needs textual installed")

from pku_sync import tui  # noqa: E402  (import after the skip guard)
from pku_sync.tui_data import Deadline, LiveCourseStatus, LiveSnapshot  # noqa: E402


def _seed(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "latest.daily.log").write_text(
        "=== pku-sync daily started 20260916_060002 ===\n=== EXIT_CODE=0 ===\n",
        encoding="utf-8",
    )
    (logs / "review_20260916.md").write_text("ok", encoding="utf-8")
    (logs / "lecture_20260916.md").write_text("ok", encoding="utf-8")
    course = tmp_path / "认知_26-27学年第1学期"
    course.mkdir()
    (course / "course.json").write_text(
        '{"course_id":"_101577_","name":"认知"}', encoding="utf-8"
    )
    (course / "assignments.md").write_text(
        "| 截止时间 | 作业 | 来源 |\n| --- | --- | --- |\n| 2026-09-20 12:00 | 作业1 | calendar |\n",
        encoding="utf-8",
    )
    (course / "deadlines.json").write_text("[]", encoding="utf-8")
    materials = course / "materials" / "第一讲"
    materials.mkdir(parents=True)
    (materials / "_index.md").write_text("# 第一讲\n\n网络分层。", encoding="utf-8")
    announcements = course / "announcements"
    announcements.mkdir()
    (announcements / "2026-09-09_通知.md").write_text("# 通知\n\n下周小测。", encoding="utf-8")
    recordings = course / "recordings"
    recordings.mkdir()
    (recordings / "index.json").write_text(
        '[{"course_id": "_101577_", "title": "2026-09-09第1-2节", "recorded_at": "2026-09-09 08:00:00"}]',
        encoding="utf-8",
    )
    rec = recordings / "2026-09-09_2026-09-09第1-2节"
    rec.mkdir()
    (rec / "notes.md").write_text("# 笔记", encoding="utf-8")


def _snapshot(*, fetched_at: datetime | None = None) -> LiveSnapshot:
    due = datetime(2026, 9, 20, 12, 0)
    return LiveSnapshot(
        fetched_at=fetched_at or datetime(2026, 9, 16, 9, 30),
        courses=(
            LiveCourseStatus(
                course="认知",
                course_id="_101577_",
                assignments=1,
                recordings=2,
                nearest_due="2026-09-20 12:00",
            ),
        ),
        deadlines=(Deadline("认知", "作业1", "2026-09-20 12:00", "calendar", due),),
    )


def test_app_constructs_with_valid_css_and_bindings():
    # Constructing the App parses the CSS; the class-level BINDINGS are plain
    # tuples (textual normalizes them into Binding objects on the instance).
    app = tui.PkuSyncApp(__import__("pathlib").Path("data"))
    keys = {binding[0] for binding in tui.PkuSyncApp.BINDINGS}
    assert keys >= {"d", "r", "b", "s", "q"}


def test_panel_builders_render_seeded_state(tmp_path):
    _seed(tmp_path)
    app = tui.PkuSyncApp(tmp_path, live_fetcher=_snapshot)

    status = app._build_status()
    assert "EXIT_CODE=0" in status
    assert "review_20260916.md" in status
    assert str(tmp_path) in status  # data source is the local DATA_DIR

    rows = app._build_ddl_rows(_snapshot(), today=datetime(2026, 9, 16))
    assert rows and rows[0][2] == "作业1" and rows[0][3] == "认知"

    course_rows = app._build_course_rows(_snapshot())
    assert course_rows and course_rows[0][0] == "认知"
    assert course_rows[0][1] == "1"
    assert course_rows[0][2] == "2026-09-20 12:00"
    assert course_rows[0][3] == "2"
    assert "正常" in course_rows[0][4]


def test_app_mounts_and_populates_all_three_panels(tmp_path):
    # Constructing the App does NOT parse its CSS in every textual version —
    # the stylesheet is only parsed when a screen registers. Mounting through
    # the headless pilot is what actually proves CSS + on_mount + the refresh
    # worker wiring (a bad CSS property or a wrong query_one id dies here).
    import asyncio

    _seed(tmp_path)

    async def scenario():
        app = tui.PkuSyncApp(tmp_path, live_fetcher=lambda **_: _snapshot())
        async with app.run_test(size=(120, 45)) as pilot:
            courses = app.query_one("#courses")
            for _ in range(20):  # the refresh worker fills tables asynchronously
                await pilot.pause(0.05)
                if courses.row_count:
                    break
            assert courses.row_count == 1
            assert "教学网" in (courses.border_title or "")
            assert app.query_one("#ddl").row_count >= 1
            assert "实时读取 1 门课" in str(app.query_one("#status").render())

    asyncio.run(scenario())


def test_live_failure_is_visible_and_does_not_use_seeded_local_course(tmp_path):
    import asyncio

    _seed(tmp_path)

    def fail(progress_fn=None):
        raise RuntimeError("IAAA unavailable")

    async def scenario():
        app = tui.PkuSyncApp(tmp_path, live_fetcher=fail)
        async with app.run_test(size=(120, 45)) as pilot:
            courses = app.query_one("#courses")
            for _ in range(20):
                await pilot.pause(0.05)
                if courses.row_count:
                    break
            assert courses.row_count == 1
            assert "读取失败" in (courses.border_title or "")
            assert "认知" not in str(list(courses.rows.values()))
            assert "IAAA unavailable" in app._live_error

    asyncio.run(scenario())


def test_refresh_replaces_previous_live_rows(tmp_path):
    import asyncio

    _seed(tmp_path)
    calls = 0

    def fetch(progress_fn=None):
        nonlocal calls
        calls += 1
        course = "第一次" if calls == 1 else "第二次"
        return LiveSnapshot(
            fetched_at=datetime(2026, 9, 16, 9, 30 + calls),
            courses=(LiveCourseStatus(course, f"_{calls}_1", 0, calls),),
            deadlines=(),
        )

    async def scenario():
        app = tui.PkuSyncApp(tmp_path, live_fetcher=fetch)
        async with app.run_test(size=(120, 45)) as pilot:
            for _ in range(20):
                await pilot.pause(0.05)
                if calls >= 1:
                    break
            assert app._live_snapshot.courses[0].course == "第一次"
            app.action_refresh()
            for _ in range(20):
                await pilot.pause(0.05)
                if calls >= 2 and app._live_snapshot.courses[0].course == "第二次":
                    break
            assert calls == 2
            assert app._live_snapshot.courses[0].course == "第二次"
            assert app.query_one("#courses").row_count == 1

    asyncio.run(scenario())


def test_refresh_ignores_overlap_while_live_fetch_is_running(tmp_path):
    import asyncio
    import threading

    _seed(tmp_path)
    release = threading.Event()
    calls = 0

    def fetch(progress_fn=None):
        nonlocal calls
        calls += 1
        release.wait(timeout=2)
        return _snapshot()

    async def scenario():
        app = tui.PkuSyncApp(tmp_path, live_fetcher=fetch)
        async with app.run_test(size=(120, 45)) as pilot:
            for _ in range(20):
                await pilot.pause(0.01)
                if calls == 1:
                    break
            app.action_refresh()
            app.action_refresh()
            await pilot.pause(0.05)
            assert calls == 1
            release.set()
            for _ in range(20):
                await pilot.pause(0.05)
                if app._live_snapshot is not None:
                    break
            assert app._live_snapshot is not None

    asyncio.run(scenario())


def test_status_panel_renders_before_live_fetch_completes(tmp_path):
    # 启动透明度的核心承诺：本地状态秒级上屏，绝不等待教学网抓取完成
    import asyncio
    import threading

    _seed(tmp_path)
    release = threading.Event()

    def slow_fetch(progress_fn=None):
        release.wait(timeout=5)
        return _snapshot()

    async def scenario():
        app = tui.PkuSyncApp(tmp_path, live_fetcher=slow_fetch)
        async with app.run_test(size=(120, 45)) as pilot:
            for _ in range(60):
                await pilot.pause(0.05)
                if "EXIT_CODE=0" in str(app.query_one("#status").render()):
                    break
            rendered = str(app.query_one("#status").render())
            assert "EXIT_CODE=0" in rendered  # 本地状态已在屏上
            assert "教学网" in rendered  # 抓取状态行同时可见
            assert app._live_snapshot is None  # 而实时抓取尚未完成
            release.set()
            for _ in range(60):
                await pilot.pause(0.05)
                if app._live_snapshot is not None:
                    break
            assert app._live_snapshot is not None
            assert "实时读取 1 门课" in str(app.query_one("#status").render())

    asyncio.run(scenario())


def test_live_progress_callback_reaches_chrome_and_status(tmp_path):
    # 逐课进度经 call_from_thread 回到 UI 线程：边框标题与状态行都带 N/total
    import asyncio
    import threading

    _seed(tmp_path)
    release = threading.Event()

    def fetch(progress_fn=None):
        progress_fn(1, 2, "第一门")
        release.wait(timeout=5)
        return _snapshot()

    async def scenario():
        app = tui.PkuSyncApp(tmp_path, live_fetcher=fetch)
        async with app.run_test(size=(120, 45)) as pilot:
            for _ in range(60):
                await pilot.pause(0.05)
                if "1/2" in (app.query_one("#courses").border_title or ""):
                    break
            assert "1/2" in (app.query_one("#courses").border_title or "")
            assert "第一门" in (app.query_one("#courses").border_title or "")
            assert "正在读取 1/2" in str(app.query_one("#status").render())
            release.set()
            for _ in range(60):
                await pilot.pause(0.05)
                if app._live_snapshot is not None:
                    break
            assert app._live_snapshot is not None
            # 抓取完成后，迟到的进度回调不得覆盖最终渲染
            assert "临期 DDL（教学网 ·" in (app.query_one("#ddl").border_title or "")

    asyncio.run(scenario())


def test_enter_on_course_table_opens_course_detail(tmp_path):
    import asyncio

    _seed(tmp_path)

    async def scenario():
        app = tui.PkuSyncApp(tmp_path, live_fetcher=lambda **_: _snapshot())
        called = False

        def detail():
            nonlocal called
            called = True

        async with app.run_test(size=(120, 45)) as pilot:
            app.action_course_detail = detail
            courses = app.query_one("#courses")
            for _ in range(20):
                await pilot.pause(0.05)
                if courses.row_count:
                    break
            courses.focus()
            await pilot.press("enter")
            assert called

    asyncio.run(scenario())


def test_course_detail_lists_artifacts_and_selects_multiple_recordings(tmp_path):
    import asyncio

    _seed(tmp_path)

    async def scenario():
        app = tui.PkuSyncApp(tmp_path, live_fetcher=lambda **_: _snapshot())
        async with app.run_test(size=(140, 52)) as pilot:
            courses = app.query_one("#courses")
            for _ in range(20):
                await pilot.pause(0.05)
                if courses.row_count:
                    break
            courses.focus()
            await pilot.press("enter")
            detail = app.query_one("#detail")
            assert detail.row_count >= 3
            kinds = {artifact.kind for artifact in app._detail_artifacts}
            assert {"课件", "公告", "录音"} <= kinds
            recording_row = next(
                index
                for index, artifact in enumerate(app._detail_artifacts)
                if artifact.selectable_recording
            )
            detail.move_cursor(row=recording_row)
            app.action_toggle_recording()
            assert len(app._selected_recording_dirs) == 1
            selected = [
                recording
                for recording in app._detail_course.recordings
                if recording.directory in app._selected_recording_dirs
            ]
            payload = app._target_payload("quiz", app._detail_course, selected)
            assert "recording_dir=" in payload
            assert "operation=quiz" in payload

    asyncio.run(scenario())
