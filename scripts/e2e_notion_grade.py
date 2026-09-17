#!/usr/bin/env python3
"""Real Notion E2E for quiz answers, grading and wrong-answer registration."""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Keep the script runnable from a clean checkout before editable installation.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pku_sync import agent_runner, tui_data
from pku_sync.config import settings


def main() -> int:
    snapshot = tui_data.collect_local_workbench(settings.data_dir)
    selected = next(
        (
            (course, tuple(row for row in course.recordings if row.status == "notes_ready")[:2])
            for course in snapshot.courses
            if any(row.status == "notes_ready" for row in course.recordings)
        ),
        None,
    )
    if selected is None:
        print("E2E FAIL: no notes-ready recording available", file=sys.stderr)
        return 1
    course, recordings = selected
    if not recordings:
        print("E2E FAIL: no recordings selected", file=sys.stderr)
        return 1

    lines = [
        "operation=e2e",
        f"course_folder={course.folder}",
        f"course_id={course.course_id}",
        f"course_name={course.name}",
        f"data_root={settings.data_dir}",
    ]
    for recording in recordings:
        lines.extend(
            [
                f"recording_dir={recording.directory}",
                f"recording_title={recording.title}",
                f"recorded_at={recording.recorded_at}",
            ]
        )
    quiz = agent_runner.run_quiz_builder(settings, "\n".join(lines), timeout=600)
    quiz_url = re.search(r"TUI_RESULT=success url=(\S+)", quiz.output)
    if quiz.returncode != 0 or quiz_url is None:
        print(f"E2E FAIL: quiz setup failed: {quiz.output[-2000:]}", file=sys.stderr)
        return 1
    page_url = quiz_url.group(1)

    target = [
        "operation=e2e_quiz_answer_setup",
        f"page_url={page_url}",
        f"course_folder={course.folder}",
        f"course_id={course.course_id}",
        f"course_name={course.name}",
        *[f"recording_dir={recording.directory}" for recording in recordings],
    ]
    answer_setup = agent_runner.run_e2e_quiz_answer_setup(settings, "\n".join(target))
    if answer_setup.returncode != 0 or "E2E_SETUP=success" not in answer_setup.output:
        print(f"E2E FAIL: answer setup failed: {answer_setup.output[-2000:]}", file=sys.stderr)
        return 1

    grade_target = "\n".join(
        [
            "operation=e2e_grade",
            f"page_url={page_url}",
            f"course_folder={course.folder}",
            f"course_id={course.course_id}",
            f"course_name={course.name}",
            *[f"recording_dir={recording.directory}" for recording in recordings],
        ]
    )
    first = agent_runner.run_quiz_grader(settings, grade_target, timeout=900)
    if first.returncode != 0 or "TUI_RESULT=success" not in first.output:
        print(f"E2E FAIL: grading failed: {first.output[-2000:]}", file=sys.stderr)
        return 1
    second = agent_runner.run_quiz_grader(settings, grade_target, timeout=900)
    second_url = re.search(r"TUI_RESULT=success url=(\S+)", second.output)
    if second.returncode != 0 or second_url is None or second_url.group(1) != page_url:
        print(f"E2E FAIL: grading was not idempotent: {second.output[-2000:]}", file=sys.stderr)
        return 1

    verification = agent_runner.run_e2e_grade_verifier(settings, "\n".join(["operation=verify_grade", *target[1:]]))
    if verification.returncode != 0 or "E2E_VERIFY=success" not in verification.output:
        print(f"E2E FAIL: grade verification failed: {verification.output[-2000:]}", file=sys.stderr)
        return 1
    print(f"E2E PASS: quiz answer fixture, grading, wrong answers, idempotent rerun, and verification {page_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
