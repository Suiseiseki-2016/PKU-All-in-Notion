"""Orchestration shared by the CLI and the future per-user panel (M0).

Owns the daily/automate contracts that used to live inside the Typer
commands (``cli.daily_cmd`` / ``cli.automate_cmd``):

- daily-log ownership: ``logs/daily_<stamp>.log`` + ``logs/latest.daily.log``
  carry the run header, the ``=== EXIT_CODE=<n> ===`` footer and the summary
  tail. ``summarize`` and the MCP review's ``get_daily_status`` read exactly
  these files, so only this layer may write them.
- failure semantics: download/process failures are swallowed, a sync failure
  (or any unexpected exception) fails the run, and the summary step can never
  make a run worse.
- automate semantics: a failed daily still lets the review and the lecture
  backfill run, because the review agent reads the EXIT_CODE marker the
  failed daily just wrote — that is what turns a silent 06:00 failure into
  a visible ⚠️ record in Notion. The daily failure rides back in the result;
  the caller (CLI) re-raises it so a scheduler's last-run status stays
  non-zero.

The module imports neither typer nor rich. Steps are injected callables and
the log recorder is an injected object (the CLI passes its rich console;
tests and the panel pass their own). Steps signal a controlled failure by
raising an exception carrying an ``exit_code`` attribute — exactly what
``typer.Exit`` (a RuntimeError subclass) does — and any other exception is
an unexpected failure. See docs/SERVICE_PLAN.md §4 (M0).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from .config import Settings

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Plain text out of rich's captured output (tables render fine bare)."""
    return ANSI_RE.sub("", text)


def write_daily_logs(logs_dir: Path, stamp: str, text: str) -> None:
    # ``latest.daily.log`` used to be daily.ps1's job (a PowerShell-level
    # redirect + copy). Retiring daily.ps1 froze the file at a stale copy,
    # which both the native summarizer and the MCP review's daily_status
    # read. The Python daily now owns its log: the recorder keeps the
    # console output, and the run writes ``logs/daily_<stamp>.log`` +
    # ``logs/latest.daily.log``.
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / f"daily_{stamp}.log").write_text(text, "utf-8")
    (logs_dir / "latest.daily.log").write_text(text, "utf-8")


class Recorder(Protocol):
    """Where a run's console-style lines go until they become the daily log."""

    def begin(self) -> None:
        """Start a fresh capture."""

    def print(self, text: str) -> None:
        """Emit one line; ``text`` may carry rich markup (stripped for the log)."""

    def end(self) -> str:
        """End the capture and return everything printed since ``begin()``."""


@dataclass(frozen=True)
class DailySteps:
    """The four pipeline steps ``run_daily`` orchestrates, already bound."""

    sync: Callable[[], None]
    download: Callable[[], None]
    process: Callable[[], None]
    summarize: Callable[[], None]


@dataclass
class DailyResult:
    exit_code: int = 0
    failure: BaseException | None = None
    stamp: str = ""


@dataclass(frozen=True)
class AutomateEvents:
    """Human-facing announcements; the CLI renders them, tests record them."""

    daily_failed: Callable[[], None]
    review_failed: Callable[[], None]
    lecture_failed: Callable[[], None]


@dataclass
class AutomateResult:
    daily: DailyResult
    review_failed: bool = False
    lecture_failed: bool = False


def _exit_code_of(exc: BaseException) -> int | None:
    """A step's controlled exit code, or None for an unexpected failure."""
    return getattr(exc, "exit_code", None)


def run_daily(
    settings: "Settings | None" = None,
    *,
    skip_process: bool = False,
    skip_summary: bool = False,
    recorder: Recorder,
    steps: DailySteps,
) -> DailyResult:
    """One unattended pass: sync, download, process, then summarize.

    Writes both daily log files and returns the outcome; the CALLER decides
    whether a failure re-raises (the CLI does, so the exit code also reaches
    whatever scheduler started the run).
    """
    if settings is None:
        from .config import settings as default_settings

        settings = default_settings
    logs_dir = settings.data_dir / "logs"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    recorder.begin()
    recorder.print(f"=== pku-sync daily started {stamp} ===")
    exit_code = 0
    failure: BaseException | None = None
    try:
        steps.sync()
        try:
            steps.download()
        except Exception as exc:  # a failed download must not stop the chain
            if _exit_code_of(exc) is None:
                raise
        if not skip_process:
            try:
                steps.process()
            except Exception as exc:  # transcription waits; the sync stands
                if _exit_code_of(exc) is None:
                    raise
    except Exception as exc:
        exit_code = _exit_code_of(exc)
        if exit_code is None:
            exit_code = 1
            recorder.print(f"[red]daily 异常终止：{exc}[/red]")
        failure = exc
    log_text = strip_ansi(recorder.end()) + f"\n=== EXIT_CODE={exit_code} ===\n"
    write_daily_logs(logs_dir, stamp, log_text)
    result = DailyResult(exit_code=exit_code, failure=failure, stamp=stamp)
    if failure is not None:
        return result  # the summary never runs for a failed pass
    if not skip_summary:
        # The summarizer reads logs/latest.daily.log, so it now summarizes
        # exactly the run that just finished; its own output is appended to
        # both log files afterwards, like daily.ps1 did.
        recorder.begin()
        try:
            steps.summarize()
        except Exception as exc:
            # The summary is a convenience layer: a failing LLM backend must
            # never turn a successful sync into a failed daily run.
            recorder.print(f"[yellow]简报生成失败: {exc}[/yellow]")
        tail = strip_ansi(recorder.end())
        if tail.strip():
            for name in (f"daily_{stamp}.log", "latest.daily.log"):
                with (logs_dir / name).open("a", encoding="utf-8") as handle:
                    handle.write(tail)
    return result


def run_automate(
    settings: "Settings | None" = None,
    *,
    skip_process: bool = False,
    skip_summary: bool = False,
    skip_review: bool = False,
    skip_lecture: bool = False,
    recorder: Recorder,
    steps: DailySteps,
    review: Callable[[], None],
    lecture: Callable[[], None],
    events: AutomateEvents,
) -> AutomateResult:
    """The daily pipeline, then the MCP review and the lecture-page backfill.

    A failed daily must not take the Notion steps down with it: daily has
    already written its EXIT_CODE marker to logs/latest.daily.log, and the
    review agent reads exactly that file — so letting review run is what
    turns a silent 06:00 failure into a visible ⚠️ record in Notion. The
    daily failure rides back in ``result.daily.failure`` for the caller to
    re-raise after the Notion steps finish.
    """
    daily = run_daily(
        settings,
        skip_process=skip_process,
        skip_summary=skip_summary,
        recorder=recorder,
        steps=steps,
    )
    result = AutomateResult(daily=daily)
    if daily.failure is not None:
        events.daily_failed()
    if not skip_review:
        try:
            review()
        except Exception as exc:
            # Local sync remains authoritative; an MCP/auth outage is reported
            # but must not turn downloaded coursework into a failed run.
            if _exit_code_of(exc) is None:
                raise
            result.review_failed = True
            events.review_failed()
    if not skip_lecture:
        try:
            lecture()
        except Exception as exc:
            # The backfill is idempotent: anything missed this round is
            # picked up again tomorrow, so a failure is logged, not fatal.
            if _exit_code_of(exc) is None:
                raise
            result.lecture_failed = True
            events.lecture_failed()
    return result
