"""The interactive console: ``pku-sync tui`` (textual).

A thin rendering layer over ``tui_data``: the left panel reads the local
pipeline-health logs, while the deadline and course panels authenticate to
the Teaching Network and parse current website content on startup and
refresh (``tui_data.fetch_live_snapshot``, with no local ``DATA_DIR``
fallback). The local workbench also lists saved materials, announcements,
recording stages and the latest Notion-agent reports before the network
finishes. Live fetch reports per-course progress (``N/total`` plus elapsed
seconds) into the panel borders while it runs, several courses at a time.
AI actions launch constrained runbooks through the configured Factory,
Claude or Codex host; they do not hold Notion credentials in the TUI.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from . import tui_data

_TRIGGER_CMDS = {
    "daily": ("daily", "pku-sync daily（同步/下载/转写/简报）"),
    "review": ("review", "pku-sync review（Notion 晨检）"),
    "lecture-batch": ("lecture-batch", "pku-sync lecture-batch（批量建页）"),
}


class PkuSyncApp(App[None]):
    """Status left, deadlines right, course table below, action output last."""

    TITLE = "pku-sync 控制台"
    CSS = """
    #status { width: 46; height: auto; border: round $primary; padding: 0 1; }
    #ddl { height: 1fr; border: round $primary; }
    #courses { height: 15; border: round $primary; }
    #detail { height: 12; border: round $accent; }
    #out { height: 14; border: round $accent; padding: 0 1; }
    """
    BINDINGS = [
        ("d", "run_daily", "daily"),
        ("r", "run_review", "晨检"),
        ("b", "run_lecture", "建页"),
        ("a", "organize_notes", "整理录音"),
        ("o", "reorganize_notion", "重整评论"),
        ("z", "build_quiz", "生成小测"),
        ("g", "grade_quiz", "批改小测"),
        ("n", "notion_status", "Notion状态"),
        ("enter", "course_detail", "课程详情"),
        ("space", "toggle_recording", "勾选录音"),
        ("s", "refresh", "刷新"),
        ("q", "quit", "退出"),
    ]

    def __init__(
        self,
        data_dir: Path,
        live_fetcher: Callable[..., tui_data.LiveSnapshot] | None = None,
    ) -> None:
        super().__init__()
        self.data_dir = data_dir.expanduser()
        self._job_running = False
        self._live_fetcher = live_fetcher or tui_data.fetch_live_snapshot
        self._live_snapshot: tui_data.LiveSnapshot | None = None
        self._live_error = ""
        self._live_loading = False
        self._status_text = ""
        self._live_progress = ""
        self._live_started_at = 0.0
        self._refresh_generation = 0
        self._local_workbench = tui_data.collect_local_workbench(self.data_dir)
        self._course_order: list[str] = []
        self._agent_running = False
        self._detail_course: tui_data.LocalCourseStatus | None = None
        self._detail_artifacts: tuple[tui_data.CourseArtifact, ...] = ()
        self._selected_recording_dirs: set[str] = set()

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Static("", id="status")
            yield DataTable(id="ddl")
        yield DataTable(id="courses")
        yield DataTable(id="detail")
        yield RichLog(id="out", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#ddl", DataTable)
        table.cursor_type = "row"
        table.add_columns("剩余", "截止", "作业", "课程")
        courses = self.query_one("#courses", DataTable)
        courses.cursor_type = "row"
        courses.add_columns(
            "课程", "作业", "最近截止", "录音", "读取状态",
            "课件", "公告", "已整理", "待整理",
        )
        # border_title is a widget attribute, not a CSS property, in the
        # textual versions we support.
        courses.border_title = "课程状态（教学网实时）"
        detail = self.query_one("#detail", DataTable)
        detail.cursor_type = "row"
        detail.add_columns("选", "类型", "标题", "摘要", "本地路径")
        detail.border_title = "课程详情（Enter 打开；Space 勾选录音）"
        self.query_one("#out", RichLog).write(
            "快捷键：d=daily r=晨检 b=建页 a=整理录音 o=重整/评论 z=生成小测 "
            "g=批改 n=Notion状态 Enter=课程详情 Space=勾选录音 s=刷新 q=退出"
        )
        self._render_local_courses()
        self._render_notion_status()
        self.action_refresh()

    # ------------------------------------------------------------ panels

    def action_refresh(self) -> None:
        # A cancelled asyncio.to_thread call keeps running in its worker
        # thread. Reject overlap instead of spawning multiple simultaneous
        # Blackboard logins when the user presses ``s`` repeatedly.
        if self._live_loading:
            return
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._live_loading = True
        self._live_error = ""
        self._live_progress = ""
        self._live_started_at = time.monotonic()
        self._update_live_chrome("正在连接教学网…")

        async def refresh() -> None:
            status_task = asyncio.create_task(asyncio.to_thread(self._build_status))
            live_task = asyncio.create_task(
                asyncio.to_thread(self._fetch_live_with_progress, generation)
            )
            try:
                try:
                    status_result = await status_task
                except Exception as exc:  # noqa: BLE001 - surfaced in the status panel
                    status_text = f"[red]管道状态读取失败：{exc}[/red]"
                else:
                    status_text = status_result
                self._status_text = status_text
                self._render_status()

                try:
                    live_result = await live_task
                except Exception as exc:  # noqa: BLE001 - surfaced in the live panels
                    self._live_snapshot = None
                    self._live_error = str(exc) or type(exc).__name__
                    self._render_live_failure(self._live_error)
                    self.query_one("#out", RichLog).write(
                        f"[red]教学网实时读取失败：{self._live_error}[/red]"
                    )
                else:
                    self._live_snapshot = live_result
                    self._live_error = ""
                    self._render_live_snapshot(live_result)
                    if live_result.warnings:
                        self.query_one("#out", RichLog).write(
                            "[yellow]教学网读取警告：" + "；".join(live_result.warnings) + "[/yellow]"
                        )
            except BaseException:
                # ``to_thread`` cannot stop an already-running worker. Cancel
                # both asyncio wrappers and let late progress callbacks die
                # at the generation/loading guard below.
                for task in (status_task, live_task):
                    if not task.done():
                        task.cancel()
                raise
            finally:
                if generation == self._refresh_generation:
                    self._live_loading = False
                    self._render_status()

        self.run_worker(refresh(), exclusive=True)

    def _fetch_live_with_progress(self, generation: int) -> tui_data.LiveSnapshot:
        def report(done: int, total: int, course: str) -> None:
            self.call_from_thread(
                self._set_live_progress,
                generation,
                done,
                total,
                course,
            )

        return self._live_fetcher(progress_fn=report)

    def _set_live_progress(self, generation: int, done: int, total: int, course: str) -> None:
        if not self._live_loading or generation != self._refresh_generation:
            return
        elapsed = time.monotonic() - self._live_started_at
        self._live_progress = f"{done}/{total} · {course} · 已用 {elapsed:.1f}s"
        self._update_live_chrome(f"正在读取 {self._live_progress}")

    def _render_status(self) -> None:
        text = self._status_text or "[dim]正在读取本地状态…[/dim]"
        self.query_one("#status", Static).update(
            text + "\n" + self._notion_status_text() + "\n" + self._live_status_text()
        )

    def _render_notion_status(self) -> None:
        self._render_status()

    def _notion_status_text(self) -> str:
        notion = self._local_workbench.notion
        review = notion.review_report or "无晨检报告"
        lecture = notion.lecture_report or "无建页报告"
        return f"[b]Notion[/b] 晨检 {review} · 讲义 {lecture}"

    def _render_local_courses(self) -> None:
        courses = self.query_one("#courses", DataTable)
        courses.clear()
        self._course_order = []
        for course in self._local_workbench.courses:
            courses.add_row(
                course.name,
                str(course.assignment_count),
                "[dim]本地[/dim]",
                str(len(course.recordings)),
                "[cyan]本地索引[/cyan]",
                str(len(course.material_files)),
                str(len(course.announcement_files)),
                str(course.recordings_ready),
                str(course.recordings_pending),
            )
            self._course_order.append(course.course_id or course.folder)
        courses.border_title = "课程状态（本地资料先行；教学网读取中）"

    def _render_live_snapshot(self, snapshot: tui_data.LiveSnapshot) -> None:
        table = self.query_one("#ddl", DataTable)
        table.clear()
        for row in self._build_ddl_rows(snapshot):
            table.add_row(*row)
        courses = self.query_one("#courses", DataTable)
        courses.clear()
        for row in self._build_course_rows(snapshot):
            courses.add_row(*row)
        self._course_order = [status.course_id for status in snapshot.courses]
        stamp = snapshot.fetched_at.strftime("%Y-%m-%d %H:%M:%S")
        table.border_title = f"临期 DDL（教学网 · {stamp}）"
        courses.border_title = f"课程状态（教学网 · {stamp}）"

    def _render_live_failure(self, error: str) -> None:
        table = self.query_one("#ddl", DataTable)
        table.clear()
        table.add_row("失败", "", error, "")
        table.border_title = "临期 DDL（教学网读取失败，无本地兜底）"
        courses = self.query_one("#courses", DataTable)
        courses.clear()
        courses.add_row("教学网读取失败", "", "", "", error, "", "", "", "")
        courses.border_title = "课程状态（教学网读取失败，无本地兜底）"

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """DataTable consumes Enter, so handle its row event explicitly."""
        if event.data_table.id == "courses":
            self.action_course_detail()

    def _render_course_detail(self, course: tui_data.LocalCourseStatus) -> None:
        self._detail_course = course
        self._detail_artifacts = tui_data.course_artifacts(self.data_dir, course)
        detail = self.query_one("#detail", DataTable)
        detail.clear()
        for artifact in self._detail_artifacts:
            selected = "☑" if artifact.recording_directory in self._selected_recording_dirs else "☐"
            detail.add_row(selected, artifact.kind, artifact.title, artifact.summary, artifact.path)
        detail.border_title = (
            f"课程详情：{course.name}（Space 勾选录音，已选 {len(self._selected_recording_dirs)}）"
        )

    def action_toggle_recording(self) -> None:
        detail = self.query_one("#detail", DataTable)
        row = detail.cursor_row
        if row < 0 or row >= len(self._detail_artifacts):
            return
        artifact = self._detail_artifacts[row]
        if not artifact.selectable_recording:
            self.query_one("#out", RichLog).write("[yellow]只有录音行可以勾选。[/yellow]")
            return
        directory = artifact.recording_directory
        if directory in self._selected_recording_dirs:
            self._selected_recording_dirs.remove(directory)
        else:
            self._selected_recording_dirs.add(directory)
        detail.update_cell_at((row, 0), "☑" if directory in self._selected_recording_dirs else "☐")
        if self._detail_course is not None:
            detail.border_title = (
                f"课程详情：{self._detail_course.name}（Space 勾选录音，"
                f"已选 {len(self._selected_recording_dirs)}）"
            )

    def _update_live_chrome(self, message: str) -> None:
        self.query_one("#ddl", DataTable).border_title = f"临期 DDL（{message}）"
        self.query_one("#courses", DataTable).border_title = f"课程状态（{message}）"
        self._render_status()

    def _live_status_text(self) -> str:
        if self._live_loading:
            progress = f" 正在读取 {self._live_progress}" if self._live_progress else " 正在连接…"
            return f"[b]教学网[/b]{progress}"
        if self._live_error:
            return f"[b]教学网[/b] [red]读取失败（无本地兜底）：{self._live_error}[/red]"
        if self._live_snapshot is None:
            return "[b]教学网[/b] 尚未读取"
        snapshot = self._live_snapshot
        stamp = snapshot.fetched_at.strftime("%Y-%m-%d %H:%M:%S")
        partial = sum(bool(course.errors) for course in snapshot.courses)
        warning = f"，{partial} 门课部分失败" if partial else ""
        if snapshot.warnings:
            warning += f"，{len(snapshot.warnings)} 项全局警告"
        return f"[b]教学网[/b] {stamp} 实时读取 {len(snapshot.courses)} 门课{warning}"

    def _read_text(self, relative: Path) -> str | None:
        """One status file under DATA_DIR; None = missing/unreadable."""
        path = self.data_dir / relative
        try:
            return path.read_text("utf-8", errors="replace")
        except OSError:
            return None

    def _build_status(self) -> str:
        today = datetime.now()
        log = self._read_text(Path("logs/latest.daily.log"))
        run = tui_data.parse_daily_log(log or "")
        run_when = "无日志" if run.started is None else run.started
        run_state = "未知（无 EXIT_CODE 标记）" if run.exit_code is None else (
            "✅ EXIT_CODE=0" if run.ok else f"❌ EXIT_CODE={run.exit_code}"
        )
        logs = self.data_dir / "logs"
        review_name = tui_data.latest_report(logs, "review")
        lecture_name = tui_data.latest_report(logs, "lecture")
        lines = [
            f"[b]数据目录[/b] {self.data_dir}",
            f"[b]最近 daily[/b] {run_when}  {run_state}",
            f"[b]最近晨检[/b] {review_name or '无记录'}（{tui_data.freshness(tui_data.report_date(review_name), today)}）",
            f"[b]最近建页[/b] {lecture_name or '无记录'}（{tui_data.freshness(tui_data.report_date(lecture_name), today)}）",
        ]
        return "\n".join(lines)

    def _build_ddl_rows(
        self,
        snapshot: tui_data.LiveSnapshot,
        today: datetime | None = None,
    ) -> list[tuple[str, str, str, str]]:
        today = today or datetime.now()
        deadlines = list(snapshot.deadlines)
        rows: list[tuple[str, str, str, str]] = []
        for deadline, left in tui_data.upcoming(deadlines, today=today, days=14):
            mark = "过期" if left < 0 else ("今天" if left == 0 else f"{left}天")
            style = "#f6c9c0" if left < 0 else ("#ffe08a" if left <= 3 else "#b8e6c1")
            rows.append((f"[{style}]{mark}[/]", deadline.due_at[:16], deadline.title, deadline.course))
        undated = [d for d in deadlines if not d.dated]
        if undated:
            titles = "、".join(d.title for d in undated[:3])
            more = "" if len(undated) <= 3 else f" 等{len(undated)}项"
            rows.append(("[dim]待公布[/]", "未公布", titles + more, ""))
        return rows

    def _build_course_rows(self, snapshot: tui_data.LiveSnapshot) -> list[tuple[str, ...]]:
        """One row per live course plus local material/recording counts."""
        rows: list[tuple[str, ...]] = []
        local_by_id = {course.course_id: course for course in self._local_workbench.courses}
        local_by_name = {course.name: course for course in self._local_workbench.courses}
        for status in snapshot.courses:
            state = "[green]正常[/green]" if not status.errors else (
                "[yellow]" + "；".join(status.errors) + "[/yellow]"
            )
            local = local_by_id.get(status.course_id) or local_by_name.get(status.course)
            rows.append(
                (
                    status.course,
                    str(status.assignments),
                    status.nearest_due or "[dim]未公布[/dim]",
                    str(status.recordings),
                    state,
                    str(len(local.material_files)) if local else "0",
                    str(len(local.announcement_files)) if local else "0",
                    str(local.recordings_ready) if local else "0",
                    str(local.recordings_pending) if local else "0",
                )
            )
        return rows

    def _selected_local_course(self) -> tui_data.LocalCourseStatus | None:
        table = self.query_one("#courses", DataTable)
        row = table.cursor_row
        if row < 0 or row >= len(self._course_order):
            return None
        key = self._course_order[row]
        for course in self._local_workbench.courses:
            if key in {course.course_id, course.folder, course.name}:
                return course
        if self._live_snapshot is not None:
            live = next(
                (item for item in self._live_snapshot.courses if item.course_id == key),
                None,
            )
            if live is not None:
                return next(
                    (course for course in self._local_workbench.courses if course.name == live.course),
                    None,
                )
        return None

    def _target_payload(
        self,
        operation: str,
        course: tui_data.LocalCourseStatus,
        recordings: list[tui_data.LocalRecordingStatus],
    ) -> str:
        lines = [
            f"operation={operation}",
            f"course_folder={course.folder}",
            f"course_id={course.course_id}",
            f"course_name={course.name}",
            f"data_root={self.data_dir}",
        ]
        for recording in recordings:
            lines += [
                f"recording_dir={recording.directory}",
                f"recording_title={recording.title}",
                f"recorded_at={recording.recorded_at}",
            ]
        return "\n".join(lines)

    def _run_agent_operation(self, operation: str) -> None:
        if self._agent_running:
            self.query_one("#out", RichLog).write("[yellow]已有 AI/Notion 操作在跑。[/yellow]")
            return
        course = self._selected_local_course()
        if course is None:
            self.query_one("#out", RichLog).write("[yellow]请先在课程表选中一门课。[/yellow]")
            return

        recordings = [
            item for item in course.recordings if item.directory in self._selected_recording_dirs
        ]
        if not recordings:
            self.query_one("#out", RichLog).write(
                "[yellow]先按 Enter 打开详情，再用 Space 勾选一讲或多讲录音。[/yellow]"
            )
            return
        if operation == "organize":
            recordings = [item for item in recordings if item.status != "notes_ready"]
            if not recordings:
                self.query_one("#out", RichLog).write(
                    "[yellow]勾选的录音都已有讲义；按 o 可读取评论后重整。[/yellow]"
                )
                return
        if operation == "reorganize":
            recordings = [item for item in recordings if item.status == "notes_ready"]
            if not recordings:
                self.query_one("#out", RichLog).write("[yellow]勾选的录音还没有可重整的讲义。[/yellow]")
                return

        from . import agent_runner
        from .config import settings

        runners = {
            "organize": agent_runner.run_notes_organizer,
            "reorganize": agent_runner.run_notion_reorganizer,
            "quiz": agent_runner.run_quiz_builder,
            "grade": agent_runner.run_quiz_grader,
        }
        labels = {
            "organize": "AI 整理录音并写入 Notion",
            "reorganize": "AI 读取评论并重整 Notion 讲次页",
            "quiz": "AI 按课程范围生成 Notion 小测",
            "grade": "AI 批改 Notion 小测答案",
        }
        payload = self._target_payload(operation, course, recordings)

        async def run_agent() -> None:
            self._agent_running = True
            log = self.query_one("#out", RichLog)
            log.write(f"[b]▶ {labels[operation]}：{course.name}[/b]")
            try:
                result = await asyncio.to_thread(runners[operation], settings, payload)
                if not result.tui_succeeded:
                    reason = result.stderr[-500:] or " ".join(result.output.split())[-700:]
                    log.write(
                        f"[red]未执行 Notion 操作（退出码 {result.returncode}）[/red] {reason}"
                    )
                else:
                    log.write(f"[green]完成[/green] 报告：{result.output_path}")
                    excerpt = result.output_path.read_text("utf-8", errors="replace")
                    log.write("  " + " ".join(excerpt.split())[-900:])
            except Exception as exc:  # noqa: BLE001 - surfaced in the TUI
                log.write(f"[red]启动失败：{exc}[/red]")
            finally:
                self._agent_running = False
                self._local_workbench = tui_data.collect_local_workbench(self.data_dir)
                self._render_local_courses()
                self._render_notion_status()
                self.action_refresh()

        self.run_worker(run_agent())

    def action_organize_notes(self) -> None:
        self._run_agent_operation("organize")

    def action_reorganize_notion(self) -> None:
        self._run_agent_operation("reorganize")

    def action_build_quiz(self) -> None:
        self._run_agent_operation("quiz")

    def action_grade_quiz(self) -> None:
        self._run_agent_operation("grade")

    def action_notion_status(self) -> None:
        self._local_workbench = tui_data.collect_local_workbench(self.data_dir)
        self._render_notion_status()
        notion = self._local_workbench.notion
        log = self.query_one("#out", RichLog)
        log.write(
            f"[b]Notion 状态[/b] 晨检：{notion.review_report or '无'}；"
            f"讲义：{notion.lecture_report or '无'}"
        )
        if notion.review_excerpt:
            log.write("[dim]晨检摘要： " + notion.review_excerpt + "[/dim]")
        if notion.lecture_excerpt:
            log.write("[dim]建页摘要： " + notion.lecture_excerpt + "[/dim]")

    def action_course_detail(self) -> None:
        course = self._selected_local_course()
        if course is None:
            return
        if self._detail_course is None or self._detail_course.folder != course.folder:
            self._selected_recording_dirs.clear()
        self._render_course_detail(course)
        self.query_one("#detail", DataTable).focus()
        log = self.query_one("#out", RichLog)
        log.write(f"[b]课程详情：{course.name}[/b]（{course.folder}）")
        log.write(
            f"课件 {len(course.material_files)} 个，公告 {len(course.announcement_files)} 个，"
            f"作业 {course.assignment_count} 个，录音 {len(course.recordings)} 个；"
            "详情表可看摘要和本地路径，Space 勾选多个录音后再按 a/o/z/g。"
        )

    # ------------------------------------------------------------ triggers

    def _trigger(self, key: str) -> None:
        if self._job_running:
            self.query_one("#out", RichLog).write("[yellow]已有任务在跑，先等它结束。[/yellow]")
            return
        command, label = _TRIGGER_CMDS[key]

        async def run_job() -> None:
            self._job_running = True
            log = self.query_one("#out", RichLog)
            log.write(f"[b]▶ {label}[/b]")
            root = str(Path(__file__).resolve().parent.parent)
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "pku_sync.cli",
                    command,
                    cwd=root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                assert process.stdout is not None
                async for raw in process.stdout:
                    log.write(raw.decode("utf-8", errors="replace").rstrip())
                code = await process.wait()
                log.write(f"[green]完成（退出码 {code}）[/green]" if code == 0 else f"[red]失败（退出码 {code}）[/red]")
            except Exception as exc:  # noqa: BLE001 - surfaced, never crashing the UI
                log.write(f"[red]启动失败：{exc}[/red]")
            finally:
                self._job_running = False
                self._local_workbench = tui_data.collect_local_workbench(self.data_dir)
                self._render_local_courses()
                self._render_notion_status()
                self.action_refresh()

        self.run_worker(run_job())

    def action_run_daily(self) -> None:
        self._trigger("daily")

    def action_run_review(self) -> None:
        self._trigger("review")

    def action_run_lecture(self) -> None:
        self._trigger("lecture-batch")
