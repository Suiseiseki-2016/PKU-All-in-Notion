"""Unit tests for the CLI's thin daily/automate wrappers (no network, no real sync).

The orchestration contracts themselves (log ownership, failure semantics)
are tested directly against `pku_sync.service` in `tests/test_service.py`;
these tests pin the wrapper path through the real rich console recorder.
"""

from __future__ import annotations

from types import SimpleNamespace

import pku_sync.cli as cli


def test_daily_cmd_writes_fresh_latest_log(tmp_path, monkeypatch):
    # daily.ps1's retirement left latest.daily.log frozen; the Python daily
    # must own the file so summarize and the MCP review read current state.
    monkeypatch.setattr(cli, "sync_cmd", lambda **kw: None)
    monkeypatch.setattr(cli, "download_cmd", lambda **kw: None)
    monkeypatch.setattr(cli, "process_cmd", lambda **kw: None)
    monkeypatch.setattr(cli, "summarize_cmd", lambda **kw: None)
    monkeypatch.setattr(
        "pku_sync.config.settings", SimpleNamespace(data_dir=tmp_path)
    )
    cli.daily_cmd()
    log = (tmp_path / "logs" / "latest.daily.log").read_text("utf-8")
    assert "=== pku-sync daily started" in log
    assert "=== EXIT_CODE=0 ===" in log
    assert len(list((tmp_path / "logs").glob("daily_*.log"))) == 1


def test_daily_cmd_records_failure_and_reraises(tmp_path, monkeypatch):
    import typer

    def boom(**kw):
        raise typer.Exit(3)

    monkeypatch.setattr(cli, "sync_cmd", boom)
    monkeypatch.setattr(cli, "download_cmd", lambda **kw: None)
    monkeypatch.setattr(cli, "process_cmd", lambda **kw: None)
    monkeypatch.setattr(
        "pku_sync.config.settings", SimpleNamespace(data_dir=tmp_path)
    )
    try:
        cli.daily_cmd(skip_summary=True)
        raised = False
    except typer.Exit as exc:
        raised = True
        assert exc.exit_code == 3
    assert raised
    log = (tmp_path / "logs" / "latest.daily.log").read_text("utf-8")
    assert "=== EXIT_CODE=3 ===" in log


def test_automate_runs_notion_steps_even_when_daily_fails(tmp_path, monkeypatch):
    # The 06:00 chain used to abort before the review when daily failed, so a
    # broken pipeline left no trace in Notion — the exact silent-failure mode
    # the daily record's 管道状态 line is meant to surface.
    import typer

    def boom(**kw):
        raise typer.Exit(3)

    calls = {"review": 0, "lecture": 0}

    monkeypatch.setattr(cli, "sync_cmd", boom)
    monkeypatch.setattr(cli, "download_cmd", lambda **kw: None)
    monkeypatch.setattr(cli, "process_cmd", lambda **kw: None)
    monkeypatch.setattr(cli, "summarize_cmd", lambda **kw: None)
    monkeypatch.setattr(
        cli, "review_cmd", lambda: calls.__setitem__("review", calls["review"] + 1)
    )
    monkeypatch.setattr(
        cli, "lecture_batch_cmd", lambda: calls.__setitem__("lecture", calls["lecture"] + 1)
    )
    monkeypatch.setattr(
        "pku_sync.config.settings", SimpleNamespace(data_dir=tmp_path)
    )
    try:
        cli.automate_cmd()
        raised = False
    except typer.Exit as exc:
        raised = True
        assert exc.exit_code == 3  # failure surfaces in schtasks Last Run Result
    assert raised
    assert calls == {"review": 1, "lecture": 1}
    log = (tmp_path / "logs" / "latest.daily.log").read_text("utf-8")
    assert "=== EXIT_CODE=3 ===" in log
