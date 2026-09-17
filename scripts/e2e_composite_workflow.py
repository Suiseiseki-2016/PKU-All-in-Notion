#!/usr/bin/env python3
"""Isolation E2E for the complete TUI lecture/quiz workflow.

This scenario drives the real Textual app with real key presses and a real
temporary DATA_DIR. The agent/MCP boundary is replaced by an in-process
store, so the test can assert page parents, lecture scope, idempotency and
grading without writing the user's Notion workspace.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

# Keep this script runnable from a clean checkout even before an editable
# install has been materialized by uv.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pku_sync import agent_runner, tui_data
from pku_sync.tui import PkuSyncApp
from pku_sync.tui_data import LiveCourseStatus, LiveSnapshot


COURSE_ID = "course-e2e"
COURSE_NAME = "组合 E2E 课程"
COURSE_PAGE_ID = "notion-course-e2e"


@dataclass
class FakeNotionStore:
    """The minimum external contract asserted by this scenario."""

    lecture_pages: dict[str, dict[str, str]] = field(default_factory=dict)
    quiz_page: dict[str, object] | None = None
    quiz_runs: int = 0
    grade_runs: int = 0
    reorganize_runs: int = 0
    block_next_quiz: bool = False

    def create_lecture(self, recording_dir: str) -> str:
        page = self.lecture_pages.setdefault(
            recording_dir,
            {"parent_id": COURSE_PAGE_ID, "recording_dir": recording_dir},
        )
        return f"https://notion.invalid/lecture/{recording_dir}"

    def create_or_reuse_quiz(self, recording_dirs: tuple[str, ...]) -> str:
        self.quiz_runs += 1
        scope = tuple(sorted(recording_dirs))
        if self.quiz_page is None:
            self.quiz_page = {
                "parent_id": COURSE_PAGE_ID,
                "course_id": COURSE_ID,
                "recording_dirs": scope,
                "questions": 4,
                "sources": list(scope),
                "answers": ["answer-1", "answer-2", "answer-3", "answer-4"],
                "grades": [],
                "wrong_answers": [],
            }
        elif self.quiz_page["recording_dirs"] != scope:
            raise AssertionError("quiz scope changed on a repeated run")
        return "https://notion.invalid/quiz/composite"

    def grade_quiz(self) -> str:
        self.grade_runs += 1
        if self.quiz_page is None:
            raise AssertionError("cannot grade before quiz creation")
        grades = self.quiz_page["grades"]
        assert isinstance(grades, list)
        if not grades:
            grades.extend([1, 0, 1, 0])
            wrong = self.quiz_page["wrong_answers"]
            assert isinstance(wrong, list)
            wrong.extend(["concept-2", "concept-4"])
        return "https://notion.invalid/quiz/composite"

    def reorganize(self) -> str:
        self.reorganize_runs += 1
        if not self.lecture_pages:
            raise AssertionError("cannot reorganize before lecture pages exist")
        return next(iter(self.lecture_pages.values()))["recording_dir"]


def _seed_data(root: Path) -> tuple[str, str]:
    course = root / "course-e2e"
    course.mkdir()
    (course / "course.json").write_text(
        json.dumps({"course_id": COURSE_ID, "name": COURSE_NAME}, ensure_ascii=False),
        encoding="utf-8",
    )
    (course / "deadlines.json").write_text("[]", encoding="utf-8")
    recordings = course / "recordings"
    recordings.mkdir()
    entries = [
        {
            "course_id": COURSE_ID,
            "title": "2026-09-09第1-2节",
            "recorded_at": "2026-09-09 08:00:00",
        },
        {
            "course_id": COURSE_ID,
            "title": "2026-09-16第3-4节",
            "recorded_at": "2026-09-16 08:00:00",
        },
    ]
    (recordings / "index.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )
    directories: list[str] = []
    for entry in entries:
        directory = tui_data._local_recordings(root, course, COURSE_ID, COURSE_NAME)[
            len(directories)
        ].directory
        directories.append(directory)
        path = root / directory
        path.mkdir(parents=True)
        (path / "transcript.json").write_text(
            json.dumps({"segments": [{"text": f"{entry['title']} 考点"}]}),
            encoding="utf-8",
        )
    return tuple(directories)  # type: ignore[return-value]


def _snapshot() -> LiveSnapshot:
    from datetime import datetime

    return LiveSnapshot(
        fetched_at=datetime(2026, 9, 16, 10, 0),
        courses=(LiveCourseStatus(COURSE_NAME, COURSE_ID, 0, 2),),
        deadlines=(),
    )


def _fake_runner(store: FakeNotionStore, root: Path, operation: str, payload: str):
    fields: dict[str, list[str]] = {}
    for line in payload.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields.setdefault(key, []).append(value)
    recording_dirs = tuple(fields.get("recording_dir", ()))
    report = root / "logs" / f"composite_{operation}.md"
    report.parent.mkdir(exist_ok=True)

    if operation == "organize":
        urls = [store.create_lecture(directory) for directory in recording_dirs]
        for directory in recording_dirs:
            note_path = root / directory / "notes.md"
            note_path.write_text(
                f"# {directory}\n\n组合 E2E 已生成讲次笔记。\n",
                encoding="utf-8",
            )
        result_url = urls[-1]
    elif operation == "quiz":
        if store.block_next_quiz:
            store.block_next_quiz = False
            report.write_text(
                "agent exited 0 without completing a Notion action\n",
                encoding="utf-8",
            )
            return agent_runner.AgentRun(
                host="fake",
                output_path=report,
                returncode=0,
                output="agent exited 0 without TUI_RESULT",
            )
        result_url = store.create_or_reuse_quiz(recording_dirs)
    elif operation == "grade":
        result_url = store.grade_quiz()
    elif operation == "reorganize":
        store.reorganize()
        result_url = "https://notion.invalid/lecture/reorganized"
    else:
        raise AssertionError(f"unexpected operation {operation}")

    report.write_text(
        f"operation={operation}\n"
        f"recording_dirs={','.join(recording_dirs)}\n"
        f"TUI_RESULT=success url={result_url}\n",
        encoding="utf-8",
    )
    return agent_runner.AgentRun(
        host="fake",
        output_path=report,
        returncode=0,
        output=f"TUI_RESULT=success url={result_url}",
    )


async def _wait_for_agent(app: PkuSyncApp, pilot) -> None:
    # Fake runs can complete between two pilot ticks, so waiting for the
    # transient True state is racy. The subsequent state/content assertions
    # prove that the operation actually ran.
    for _ in range(100):
        await pilot.pause(0.05)
        if not app._agent_running and not app._live_loading:
            return
    raise AssertionError("agent operation did not finish")


async def _scenario(root: Path) -> FakeNotionStore:
    directories = _seed_data(root)
    store = FakeNotionStore()

    def fake_runner(settings, payload: str, timeout: int = 3600):
        operation = next(
            value for value in ("organize", "quiz", "grade", "reorganize")
            if f"operation={value}" in payload
        )
        return _fake_runner(store, root, operation, payload)

    original = {
        "notes": agent_runner.run_notes_organizer,
        "quiz": agent_runner.run_quiz_builder,
        "grade": agent_runner.run_quiz_grader,
        "reorganize": agent_runner.run_notion_reorganizer,
    }
    agent_runner.run_notes_organizer = lambda settings, payload, timeout=3600: fake_runner(settings, payload, timeout)
    agent_runner.run_quiz_builder = lambda settings, payload, timeout=3600: fake_runner(settings, payload, timeout)
    agent_runner.run_quiz_grader = lambda settings, payload, timeout=3600: fake_runner(settings, payload, timeout)
    agent_runner.run_notion_reorganizer = lambda settings, payload, timeout=3600: fake_runner(settings, payload, timeout)
    try:
        app = PkuSyncApp(root, live_fetcher=lambda **_: _snapshot())
        async with app.run_test(size=(160, 52)) as pilot:
            courses = app.query_one("#courses")
            for _ in range(100):
                await pilot.pause(0.05)
                if courses.row_count:
                    break
            assert courses.row_count == 1

            courses.focus()
            await pilot.press("enter")
            detail = app.query_one("#detail")
            recording_rows = [
                index
                for index, artifact in enumerate(app._detail_artifacts)
                if artifact.selectable_recording
            ]
            assert len(recording_rows) == 2
            for row in recording_rows:
                detail.move_cursor(row=row)
                await pilot.press("space")
            assert app._selected_recording_dirs == set(directories)

            courses.focus()
            await pilot.press("a")
            await _wait_for_agent(app, pilot)
            assert set(store.lecture_pages) == set(directories)
            assert all(
                page["parent_id"] == COURSE_PAGE_ID
                for page in store.lecture_pages.values()
            )

            courses.move_cursor(row=0)
            assert app._selected_local_course() is not None
            assert app._selected_recording_dirs == set(directories)
            store.block_next_quiz = True
            app.action_build_quiz()
            await _wait_for_agent(app, pilot)
            assert store.quiz_page is None
            output_lines = "\n".join(str(line) for line in app.query_one("#out").lines)
            assert "未执行 Notion 操作" in output_lines

            courses.move_cursor(row=0)
            app.action_build_quiz()
            await _wait_for_agent(app, pilot)
            assert store.quiz_page is not None
            assert store.quiz_page["parent_id"] == COURSE_PAGE_ID
            assert store.quiz_page["recording_dirs"] == tuple(sorted(directories))
            assert store.quiz_page["questions"] == 4

            courses.move_cursor(row=0)
            app.action_build_quiz()
            await _wait_for_agent(app, pilot)
            assert store.quiz_runs == 2
            assert store.quiz_page["recording_dirs"] == tuple(sorted(directories))

            courses.move_cursor(row=0)
            app.action_grade_quiz()
            await _wait_for_agent(app, pilot)
            assert store.grade_runs == 1
            assert store.quiz_page["grades"] == [1, 0, 1, 0]
            assert store.quiz_page["wrong_answers"] == ["concept-2", "concept-4"]

            courses.move_cursor(row=0)
            app.action_reorganize_notion()
            await _wait_for_agent(app, pilot)
            assert store.reorganize_runs == 1
    finally:
        agent_runner.run_notes_organizer = original["notes"]
        agent_runner.run_quiz_builder = original["quiz"]
        agent_runner.run_quiz_grader = original["grade"]
        agent_runner.run_notion_reorganizer = original["reorganize"]
    return store


def main() -> int:
    try:
        with TemporaryDirectory(prefix="pku-e2e-") as directory:
            store = asyncio.run(_scenario(Path(directory)))
            print(
                "E2E PASS: a→z(blocked recovery)→z→z(idempotent)→g→o completed; "
                f"lectures={len(store.lecture_pages)} quiz_runs={store.quiz_runs} "
                f"grade_runs={store.grade_runs} reorganize_runs={store.reorganize_runs}"
            )
    except Exception as exc:  # noqa: BLE001 - preserve scenario failure
        print(f"E2E FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
