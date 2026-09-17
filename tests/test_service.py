"""Unit tests for the service orchestration layer (no network, no rich).

The contracts first pinned down by `tests/test_cli.py` (log ownership,
failure semantics, automate's "review runs even when daily fails") are
re-tested here against the orchestration layer itself; the CLI tests keep
covering the thin-wrapper path with the real rich console.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer

from pku_sync import service


class ListRecorder:
    """A `service.Recorder` collecting printed lines into a plain buffer."""

    def __init__(self) -> None:
        self.buffer: list[str] = []

    def begin(self) -> None:
        self.buffer = []

    def print(self, text: str) -> None:
        self.buffer.append(text)

    def end(self) -> str:
        return "\n".join(self.buffer)


def make_steps(sink: dict, **overrides) -> service.DailySteps:
    """DailySteps recording every call; individual steps replaced via overrides."""

    def make(name: str):
        def step() -> None:
            sink.setdefault("calls", []).append(name)

        return step

    built = {
        "sync": make("sync"),
        "download": make("download"),
        "process": make("process"),
        "summarize": make("summarize"),
    }
    built.update(overrides)
    return service.DailySteps(**built)


def raises(exc: BaseException):
    def step() -> None:
        raise exc

    return step


def make_events(fired: list[str]) -> service.AutomateEvents:
    return service.AutomateEvents(
        daily_failed=lambda: fired.append("daily"),
        review_failed=lambda: fired.append("review"),
        lecture_failed=lambda: fired.append("lecture"),
    )


def test_strip_ansi_removes_escape_codes():
    assert service.strip_ansi("\x1b[31mred\x1b[0m plain") == "red plain"
    assert service.strip_ansi("no codes") == "no codes"


def test_write_daily_logs_writes_dated_and_latest(tmp_path):
    service.write_daily_logs(tmp_path, "20260916_060002", "hello log")
    assert (tmp_path / "daily_20260916_060002.log").read_text("utf-8") == "hello log"
    assert (tmp_path / "latest.daily.log").read_text("utf-8") == "hello log"


def test_run_daily_writes_header_footer_and_both_files(tmp_path):
    sink: dict = {}
    result = service.run_daily(
        SimpleNamespace(data_dir=tmp_path),
        recorder=ListRecorder(),
        steps=make_steps(sink),
    )
    assert result.exit_code == 0
    assert result.failure is None
    assert sink["calls"] == ["sync", "download", "process", "summarize"]
    log = (tmp_path / "logs" / "latest.daily.log").read_text("utf-8")
    assert "=== pku-sync daily started" in log
    assert "=== EXIT_CODE=0 ===" in log
    assert len(list((tmp_path / "logs").glob("daily_*.log"))) == 1


def test_run_daily_defaults_to_config_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("pku_sync.config.settings", SimpleNamespace(data_dir=tmp_path))
    service.run_daily(recorder=ListRecorder(), steps=make_steps({}))
    assert (tmp_path / "logs" / "latest.daily.log").exists()


def test_run_daily_sync_failure_sets_exit_code_and_skips_rest(tmp_path):
    sink: dict = {}
    failure = typer.Exit(3)
    result = service.run_daily(
        SimpleNamespace(data_dir=tmp_path),
        recorder=ListRecorder(),
        steps=make_steps(sink, sync=raises(failure)),
    )
    assert result.exit_code == 3
    assert result.failure is failure
    assert sink == {}  # download/process/summarize never run (sync's raise is the proof)
    log = (tmp_path / "logs" / "latest.daily.log").read_text("utf-8")
    assert "=== EXIT_CODE=3 ===" in log
    assert "异常终止" not in log  # a controlled failure needs no alarm line


def test_run_daily_swallows_download_and_process_failures(tmp_path):
    sink: dict = {}
    result = service.run_daily(
        SimpleNamespace(data_dir=tmp_path),
        recorder=ListRecorder(),
        steps=make_steps(
            sink,
            download=raises(typer.Exit(1)),
            process=raises(typer.Exit(2)),
        ),
    )
    assert result.exit_code == 0
    assert result.failure is None
    # The overridden download/process record nothing; their failure being
    # swallowed is proven by sync and summarize still running.
    assert sink["calls"] == ["sync", "summarize"]


def test_run_daily_unexpected_failure_fails_run(tmp_path):
    sink: dict = {}
    result = service.run_daily(
        SimpleNamespace(data_dir=tmp_path),
        recorder=ListRecorder(),
        steps=make_steps(sink, sync=raises(ValueError("boom"))),
    )
    assert result.exit_code == 1
    assert isinstance(result.failure, ValueError)
    assert sink == {}  # download/process/summarize never run
    log = (tmp_path / "logs" / "latest.daily.log").read_text("utf-8")
    assert "daily 异常终止" in log
    assert "=== EXIT_CODE=1 ===" in log


def test_run_daily_summary_failure_is_not_fatal_and_tail_appended(tmp_path):
    sink: dict = {}
    result = service.run_daily(
        SimpleNamespace(data_dir=tmp_path),
        recorder=ListRecorder(),
        steps=make_steps(sink, summarize=raises(RuntimeError("llm down"))),
    )
    assert result.exit_code == 0
    assert result.failure is None
    log = (tmp_path / "logs" / "latest.daily.log").read_text("utf-8")
    assert log.index("=== EXIT_CODE=0 ===") < log.index("简报生成失败: llm down")


def test_run_daily_appends_summary_output_to_both_logs(tmp_path):
    recorder = ListRecorder()
    service.run_daily(
        SimpleNamespace(data_dir=tmp_path),
        recorder=recorder,
        steps=make_steps({}, summarize=lambda: recorder.print("简报已写入 summary.md")),
    )
    for log_file in (tmp_path / "logs").glob("*.log"):
        assert "简报已写入 summary.md" in log_file.read_text("utf-8")


def test_run_automate_runs_notion_steps_when_daily_fails(tmp_path):
    fired: list[str] = []
    result = service.run_automate(
        SimpleNamespace(data_dir=tmp_path),
        recorder=ListRecorder(),
        steps=make_steps({}, sync=raises(typer.Exit(3))),
        review=lambda: fired.append("review-run"),
        lecture=lambda: fired.append("lecture-run"),
        events=make_events(fired),
    )
    assert result.daily.exit_code == 3
    assert result.review_failed is False
    assert result.lecture_failed is False
    assert fired == ["daily", "review-run", "lecture-run"]  # review still runs


def test_run_automate_review_failure_reported_not_fatal(tmp_path):
    fired: list[str] = []
    result = service.run_automate(
        SimpleNamespace(data_dir=tmp_path),
        recorder=ListRecorder(),
        steps=make_steps({}),
        review=raises(typer.Exit(1)),
        lecture=lambda: fired.append("lecture-run"),
        events=make_events(fired),
    )
    assert result.daily.exit_code == 0
    assert result.daily.failure is None
    assert result.review_failed is True
    assert fired == ["review", "lecture-run"]


def test_run_automate_unexpected_review_error_propagates(tmp_path):
    fired: list[str] = []
    with pytest.raises(ValueError):
        service.run_automate(
            SimpleNamespace(data_dir=tmp_path),
            recorder=ListRecorder(),
            steps=make_steps({}),
            review=raises(ValueError("agent host missing")),
            lecture=lambda: fired.append("lecture-run"),
            events=make_events(fired),
        )
    assert fired == []  # lecture never ran; only controlled exits are swallowed
