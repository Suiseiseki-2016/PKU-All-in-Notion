#!/usr/bin/env python3
"""Isolation E2E for automate's daily/review/lecture orchestration.

The real CLI command is invoked directly, while the network and agent
boundaries are replaced with deterministic functions. The scenario proves the
important contract: a failed daily records EXIT_CODE, review and lecture
backfill still run, and automate finally returns a failure to the scheduler.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from typer import Exit

# Keep the script runnable from a clean checkout before an editable install.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pku_sync import cli, config


def _run_scenario(root: Path, daily_code: int) -> list[str]:
    events: list[str] = []
    logs = root / "logs"
    logs.mkdir(exist_ok=True)

    def fake_sync(*, course: str, skip_recordings: bool) -> None:
        events.append("daily(skip_process=True,skip_summary=True)")
        assert course == ""
        assert skip_recordings is False
        if daily_code:
            raise Exit(daily_code)

    def fake_download(*, course: str, limit: int) -> None:
        assert course == ""
        assert limit == 0

    def fake_review() -> None:
        events.append("review")
        status = (logs / "latest.daily.log").read_text("utf-8")
        assert f"EXIT_CODE={daily_code}" in status
        (logs / "review_20260916.md").write_text(
            f"review saw EXIT_CODE={daily_code}\n", encoding="utf-8"
        )

    def fake_lecture_batch() -> None:
        events.append("lecture-batch")
        assert (logs / "review_20260916.md").exists()
        (logs / "lecture_20260916.md").write_text(
            "lecture batch continued after review\n", encoding="utf-8"
        )

    original = (
        cli.sync_cmd,
        cli.download_cmd,
        cli.review_cmd,
        cli.lecture_batch_cmd,
        config.settings,
    )
    cli.sync_cmd = fake_sync
    cli.download_cmd = fake_download
    cli.review_cmd = fake_review
    cli.lecture_batch_cmd = fake_lecture_batch
    config.settings = SimpleNamespace(data_dir=root)
    try:
        try:
            cli.automate_cmd(
                skip_process=True,
                skip_summary=True,
                skip_review=False,
                skip_lecture=False,
            )
        except Exit as exc:
            if daily_code == 0:
                raise AssertionError(f"successful daily raised Exit({exc.exit_code})") from exc
            assert exc.exit_code == daily_code
        else:
            if daily_code:
                raise AssertionError("failed daily did not propagate its exit code")
    finally:
        (
            cli.sync_cmd,
            cli.download_cmd,
            cli.review_cmd,
            cli.lecture_batch_cmd,
            config.settings,
        ) = original
    assert events == [
        "daily(skip_process=True,skip_summary=True)",
        "review",
        "lecture-batch",
    ], events
    assert (logs / "review_20260916.md").exists()
    assert (logs / "lecture_20260916.md").exists()
    return events


def main() -> int:
    try:
        with TemporaryDirectory(prefix="pku-automate-e2e-") as directory:
            root = Path(directory)
            _run_scenario(root, daily_code=1)
            _run_scenario(root, daily_code=0)
        print(
            "E2E PASS: automate ran daily→review→lecture-batch on failure and "
            "success; EXIT_CODE was preserved and downstream steps continued"
        )
    except Exception as exc:  # noqa: BLE001 - preserve scenario failure
        print(f"E2E FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
