"""Panel-to-service wiring: run daily/automate through the M0 service layer.

The panel never re-implements orchestration; it binds the same CLI steps
and recorder the ``pku-sync daily`` / ``pku-sync automate`` commands use,
so a click on the page and a scheduled run share one code path (and one
set of log files).
"""

from __future__ import annotations

from .. import service
from .jobs import JobRunner

ACTIONS = ("daily", "automate")


def run_pipeline(settings, kind: str) -> int:
    """Run one panel action; returns the run's exit code."""
    from .. import cli

    if kind == "daily":
        result = service.run_daily(
            settings,
            recorder=cli.RichConsoleRecorder(cli.console),
            steps=cli._daily_steps(),
        )
        return result.exit_code
    if kind == "automate":
        result = service.run_automate(
            settings,
            recorder=cli.RichConsoleRecorder(cli.console),
            steps=cli._daily_steps(),
            review=cli.review_cmd,
            lecture=cli.lecture_batch_cmd,
            events=cli._automate_events(),
        )
        # Only the daily half carries the exit code: review/lecture failures
        # are reported, never fatal (same semantics as `pku-sync automate`).
        return result.daily.exit_code
    raise ValueError(f"unknown panel action: {kind}")


def make_runner(settings) -> JobRunner:
    """The default runner: the actions above, one at a time."""
    return JobRunner(lambda kind: run_pipeline(settings, kind))
