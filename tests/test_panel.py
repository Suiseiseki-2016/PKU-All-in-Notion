"""Panel tests: routing, single-flight, status parsing (TestClient, no network)."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pku_sync.panel import jobs as jobs_mod
from pku_sync.panel import webapi


class FakeRunner:
    """A runner double: records submits, reports a fixed state."""

    def __init__(self, running: bool = False):
        self.submitted: list[str] = []
        self.running = running

    def submit(self, kind: str):
        self.submitted.append(kind)
        if self.running:
            return None  # busy
        return jobs_mod.Job(kind=kind, state="running")

    def current(self):
        return None

    def last(self):
        return None


def make_settings(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=tmp_path)


def write_index(tmp_path) -> None:
    """One course with one indexed recording, the minimal real data tree."""
    rec_dir = tmp_path / "认知心理学" / "recordings"
    rec_dir.mkdir(parents=True)
    index = [
        {"course_id": "_c1_", "title": "第一讲 绪论", "recorded_at": "2026-09-09 08:00:00"}
    ]
    (rec_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False), "utf-8")


def client(tmp_path, runner=None) -> TestClient:
    return TestClient(webapi.create_app(make_settings(tmp_path), runner=runner))


def test_index_page_renders(tmp_path):
    write_index(tmp_path)
    response = client(tmp_path).get("/")
    assert response.status_code == 200
    text = response.text
    assert "PKU All in Notion 状态面板" in text
    assert "跑 daily" in text
    assert "认知心理学" in text
    assert "第一讲 绪论" in text
    # Product-GUI preview: quiz/organize actions per course + quota placeholder
    # (disabled until M2) and the Notion reports section.
    assert "生成小测" in text
    assert "云端配额：未连接平台" in text
    assert "Notion 报告" in text


def test_status_fragment_shows_recording_ladder(tmp_path):
    write_index(tmp_path)
    response = client(tmp_path).get("/partials/status")
    assert response.status_code == 200
    assert "认知心理学" in response.text
    assert "indexed" in response.text


def test_status_json_shape(tmp_path):
    write_index(tmp_path)
    payload = client(tmp_path).get("/api/status").json()
    assert payload["running"] is None
    assert payload["last"] is None
    assert payload["daily"] == {"exists": False}
    course = payload["recordings"][0]
    assert course["name"] == "认知心理学"
    assert course["recordings"][0]["stage"] == "indexed"
    assert payload["notion"] == [
        {"kind": "晨检", "file": None, "head": ""},
        {"kind": "批量建页", "file": None, "head": ""},
    ]


def test_notion_reports_read_latest(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "review_20260917.md").write_text("9月17日晨检", "utf-8")
    (logs / "review_20260918.md").write_text("9月18日晨检：登记 2 条新任务", "utf-8")
    (logs / "lecture_20260918.md").write_text("批量建页：补建 3 个讲次页", "utf-8")
    reports = webapi.notion_reports(logs)
    assert reports[0]["kind"] == "晨检"
    assert reports[0]["file"] == "review_20260918.md"
    assert "登记 2 条新任务" in reports[0]["head"]
    assert reports[1]["kind"] == "批量建页"
    assert reports[1]["file"] == "lecture_20260918.md"


def test_daily_log_marker_is_parsed(tmp_path):
    (tmp_path / "logs").mkdir(parents=True)
    (tmp_path / "logs" / "latest.daily.log").write_text(
        "=== pku-sync daily started 20260917_060000 ===\nsome output\n=== EXIT_CODE=3 ===\n",
        "utf-8",
    )
    parsed = webapi.latest_daily(tmp_path / "logs")
    assert parsed["exists"] is True
    assert parsed["exit_code"] == 3
    assert "some output" in parsed["tail"]


def test_action_submits_to_runner(tmp_path):
    runner = FakeRunner()
    response = client(tmp_path, runner=runner).post("/actions/daily")
    assert response.status_code == 200
    assert response.json() == {"started": "daily"}
    assert runner.submitted == ["daily"]


def test_action_rejected_while_busy(tmp_path):
    runner = FakeRunner(running=True)
    response = client(tmp_path, runner=runner).post("/actions/automate")
    assert response.status_code == 409
    assert runner.submitted == ["automate"]


def test_unknown_action_is_404(tmp_path):
    response = client(tmp_path).post("/actions/nap")
    assert response.status_code == 404


def test_healthz(tmp_path):
    response = client(tmp_path).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_runner_is_single_flight_and_records_results():
    started = threading.Event()
    release = threading.Event()

    def fake_run(kind: str) -> int:
        started.set()
        assert release.wait(timeout=5)
        return 0

    runner = jobs_mod.JobRunner(fake_run)
    first = runner.submit("daily")
    assert first is not None and first.state == "running"
    assert started.wait(timeout=5)
    assert runner.submit("automate") is None  # busy → rejected
    release.set()

    for _ in range(200):
        if runner.last() is not None:
            break
        time.sleep(0.02)
    last = runner.last()
    assert last is not None
    assert (last.kind, last.state, last.exit_code) == ("daily", "done", 0)
    assert runner.current() is None
    # free again after completion
    assert runner.submit("daily") is not None


def test_runner_records_pipeline_crashes():
    def broken_run(kind: str) -> int:
        raise ValueError("whisper exploded")

    runner = jobs_mod.JobRunner(broken_run)
    assert runner.submit("daily") is not None
    for _ in range(200):
        if runner.last() is not None:
            break
        time.sleep(0.02)
    last = runner.last()
    assert last.state == "failed"
    assert last.exit_code == 1
    assert "whisper exploded" in last.error
