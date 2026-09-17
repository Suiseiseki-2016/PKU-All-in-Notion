#!/usr/bin/env python3
"""Create one explicitly marked Notion quiz page through the real agent host."""

from __future__ import annotations

import re
import sys

from pku_sync import agent_runner, tui_data
from pku_sync.config import settings


def main() -> int:
    snapshot = tui_data.collect_local_workbench(settings.data_dir)
    for course in snapshot.courses:
        recordings = [row for row in course.recordings if row.status == "notes_ready"][:2]
        if recordings:
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
            payload = "\n".join(lines)
            break
    else:
        print("E2E FAIL: no notes-ready recording available", file=sys.stderr)
        return 1

    result = agent_runner.run_quiz_builder(settings, payload, timeout=600)
    if not result.tui_succeeded:
        print(f"E2E FAIL: {result.output[-2000:] or result.stderr}", file=sys.stderr)
        return 1
    url = re.search(r"TUI_RESULT=success url=(\S+)", result.output)
    if url is None:
        print("E2E FAIL: success marker has no Notion URL", file=sys.stderr)
        return 1
    second = agent_runner.run_quiz_builder(settings, payload, timeout=600)
    second_url = re.search(r"TUI_RESULT=success url=(\S+)", second.output)
    if second.returncode != 0 or second_url is None or second_url.group(1) != url.group(1):
        print(
            "E2E FAIL: repeated quiz generation did not reuse the same page: "
            f"{second.output[-1200:] or second.stderr}",
            file=sys.stderr,
        )
        return 1
    verify_payload = "\n".join(
        [
            "operation=verify_quiz",
            f"page_url={url.group(1)}",
            f"course_folder={course.folder}",
            f"course_id={course.course_id}",
            f"course_name={course.name}",
            *[f"recording_dir={recording.directory}" for recording in recordings],
        ]
    )
    verification = agent_runner.run_quiz_verifier(settings, verify_payload, timeout=600)
    if verification.returncode != 0 or "E2E_VERIFY=success" not in verification.output:
        print(
            f"E2E FAIL: quiz page verification failed: "
            f"{verification.output[-2000:] or verification.stderr}",
            file=sys.stderr,
        )
        return 1
    print(
        "E2E PASS: created, reused, and verified Notion quiz test page "
        f"{url.group(1)} scope={len(recordings)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
