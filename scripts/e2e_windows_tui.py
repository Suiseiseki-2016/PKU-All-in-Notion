#!/usr/bin/env python3
"""Windows-local E2E check for the real TUI and Teaching Network data.

Run this on the Windows execution host. It does not test SSH or use fixtures:
the default live fetcher authenticates to the actual Teaching Network and the
app reads the configured Windows course-data directory.
"""

from __future__ import annotations

import asyncio
import sys

from pku_sync.config import settings
from pku_sync.tui import PkuSyncApp


class E2EApp(PkuSyncApp):
    """Keep the real detail action, while recording that its event fired."""

    detail_opened = False

    def action_course_detail(self) -> None:
        super().action_course_detail()
        self.detail_opened = True


async def scenario() -> None:
    app = E2EApp(settings.data_dir)
    async with app.run_test(size=(160, 48)) as pilot:
        courses = app.query_one("#courses")
        for _ in range(20):
            await pilot.pause(0.25)
            if courses.row_count:
                break

        if not courses.row_count:
            raise RuntimeError("local course table did not render")
        if "Notion" not in str(app.query_one("#status").render()):
            raise RuntimeError("Notion status was not rendered")

        courses.focus()
        await pilot.press("enter")
        if not app.detail_opened:
            raise RuntimeError("Enter did not invoke the course detail action")
        detail = app.query_one("#detail")
        recording_row = next(
            (
                index
                for index, artifact in enumerate(app._detail_artifacts)
                if artifact.selectable_recording
            ),
            None,
        )
        if recording_row is None:
            raise RuntimeError("selected course has no recording available for range selection")
        detail.move_cursor(row=recording_row)
        await pilot.press("space")
        if not app._selected_recording_dirs:
            raise RuntimeError("Space did not select a recording for the AI range")

        await pilot.press("q")


def main() -> int:
    try:
        asyncio.run(scenario())
    except Exception as exc:  # noqa: BLE001 - an E2E failure needs its real cause
        print(f"E2E FAIL: {exc}", file=sys.stderr)
        return 1
    print("E2E PASS: real Windows data loaded, Enter opened details, Space selected a recording, q exited")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
