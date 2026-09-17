#!/usr/bin/env python3
"""Real Notion E2E for comment-driven lecture-page reorganization."""

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
            (course, recording)
            for course in snapshot.courses
            for recording in course.recordings
            if recording.status == "notes_ready"
        ),
        None,
    )
    if selected is None:
        print("E2E FAIL: no notes-ready recording available", file=sys.stderr)
        return 1
    course, recording = selected
    setup_payload = "\n".join(
        [
            "operation=e2e_reorganize_setup",
            f"course_folder={course.folder}",
            f"course_id={course.course_id}",
            f"course_name={course.name}",
            f"data_root={settings.data_dir}",
            f"recording_dir={recording.directory}",
            f"recording_title={recording.title}",
            f"recorded_at={recording.recorded_at}",
        ]
    )
    setup = agent_runner.run_e2e_reorganize_setup(settings, setup_payload)
    match = re.search(r"E2E_SETUP=success url=(\S+)", setup.output)
    if setup.returncode != 0 or match is None:
        print(f"E2E FAIL: setup failed: {setup.output[-2000:]}", file=sys.stderr)
        return 1
    page_url = match.group(1)

    payload = "\n".join(
        [
            "operation=e2e_reorganize",
            f"page_url={page_url}",
            f"course_folder={course.folder}",
            f"course_id={course.course_id}",
            f"course_name={course.name}",
            f"data_root={settings.data_dir}",
            f"recording_dir={recording.directory}",
            f"recording_title={recording.title}",
            f"recorded_at={recording.recorded_at}",
        ]
    )
    first = agent_runner.run_notion_reorganizer(settings, payload, timeout=900)
    first_url = re.search(r"TUI_RESULT=success url=(\S+)", first.output)
    if first.returncode != 0 or first_url is None or first_url.group(1) != page_url:
        print(f"E2E FAIL: reorganize failed: {first.output[-2000:]}", file=sys.stderr)
        return 1

    second = agent_runner.run_notion_reorganizer(settings, payload, timeout=900)
    second_url = re.search(r"TUI_RESULT=success url=(\S+)", second.output)
    if second.returncode != 0 or second_url is None or second_url.group(1) != page_url:
        print(f"E2E FAIL: reorganize was not idempotent: {second.output[-2000:]}", file=sys.stderr)
        return 1

    verification = agent_runner.run_e2e_reorganize_verifier(settings, payload)
    if verification.returncode != 0 or "E2E_VERIFY=success" not in verification.output:
        print(f"E2E FAIL: verification failed: {verification.output[-2000:]}", file=sys.stderr)
        return 1
    print(f"E2E PASS: setup, comment reorganization, idempotent rerun, and verification {page_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
