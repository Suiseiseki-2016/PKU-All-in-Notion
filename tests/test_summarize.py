"""Unit tests for the daily summary port (no LLM calls)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from pku_sync import summarize


def test_summary_path_appends_suffixes(tmp_path):
    first = summarize.summary_path(tmp_path, date(2026, 9, 15))
    assert first == tmp_path / "summary_20260915.md"
    first.write_text("x", encoding="utf-8")

    second = summarize.summary_path(tmp_path, date(2026, 9, 15))
    assert second == tmp_path / "summary_20260915-2.md"
    second.write_text("x", encoding="utf-8")

    assert summarize.summary_path(tmp_path, date(2026, 9, 15)) == tmp_path / "summary_20260915-3.md"


def test_trim_keeps_short_logs_whole():
    text = "a" * 100
    assert summarize._trim(text) == text


def test_trim_bounds_long_logs():
    text = "H" * summarize.HEAD_KEEP + "M" * 200_000 + "T" * 500
    trimmed = summarize._trim(text)
    assert len(trimmed) <= summarize.MAX_LOG_CHARS + 60
    assert trimmed.startswith("H" * summarize.HEAD_KEEP)
    assert "中间已截断" in trimmed
    assert trimmed.endswith("T" * 500)


def test_build_user_prompt_fences_log():
    prompt = summarize.build_user_prompt("EXIT_CODE=0")
    assert "```" in prompt and "EXIT_CODE=0" in prompt


def test_run_returns_none_without_log(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path)
    assert summarize.run(settings=settings) is None


def test_run_writes_llm_answer(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "latest.daily.log").write_text("=== daily ok ===\nEXIT_CODE=0\n", encoding="utf-8")
    monkeypatch.setattr(
        "pku_sync.llm.complete", lambda system, user, settings=None: "**同步结果**\n- 测试"
    )
    settings = SimpleNamespace(data_dir=tmp_path)

    path = summarize.run(settings=settings)

    assert path is not None and path.exists()
    assert path.read_text("utf-8").startswith("**同步结果**")
    assert path.name.startswith(f"summary_{date.today():%Y%m%d}")
