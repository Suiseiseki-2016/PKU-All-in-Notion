"""Fault-injected relay coverage for the student grading panel."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from pku_sync.platform import PlatformError
from pku_sync.panel.exercise_grader import ExerciseGrader, GradingInput
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import EXERCISE_GENERATED
from pku_sync.panel.webapi import create_app


@dataclass
class FaultRelay:
    statuses: list[int] = field(default_factory=list)
    balance: float = 20.0
    charge: float = 2.0

    def __post_init__(self):
        self.calls: list[str] = []

    def quota(self):
        return {"active": True, "available": True, "llm_points_remaining": self.balance}

    def grade(self, prompt):
        self.calls.append("grade")
        if self.statuses:
            status = self.statuses.pop(0)
            error = PlatformError({
                401: "云端登录已失效，请重新激活后重试。",
                402: "AI 点不足，请兑换新额度后重试。",
                502: "AI 服务暂时没有完成请求，请稍后重试。",
                503: "AI 服务暂未配置，请联系管理员后重试。",
            }[status])
            error.status_code = status
            raise error
        self.balance -= self.charge
        return {"content": json.dumps({"score": 88}), "points_charged": self.charge}


class WriteTrackingAdapter:
    def __init__(self):
        self.read_calls: list[str] = []
        self.write_calls: list[dict] = []

    def read_for_grading(self, target):
        self.read_calls.append(target.page_id)
        return GradingInput(prompt="grade", unanswered=[])

    def write_result(self, target, *, content, score, graded_at):
        self.write_calls.append({"page_id": target.page_id, "score": score})


def make_client(relay: FaultRelay):
    directory = build_fake_directory()
    adapter = WriteTrackingAdapter()
    grader = ExerciseGrader(directory_service=directory, relay=relay, page_adapter=adapter)
    client = TestClient(create_app(directory_service=directory, platform_service=relay, grading_service=grader))
    assert client.get("/api/directory").status_code == 200
    return client, directory, adapter


def wait_for_grade(client: TestClient, job_id: str) -> dict:
    for _ in range(200):
        result = client.get("/api/exercises/grade/status", params={"job_id": job_id}).json()
        if result.get("status") != "running":
            return result
        time.sleep(0.005)
    pytest.fail("grade job did not finish")


@pytest.mark.parametrize(("relay_status", "expected_reason"), [(401, "重新激活"), (402, "兑换新额度"), (503, "暂未配置")])
def test_relay_blocked_statuses_keep_pending_state_and_never_write(relay_status, expected_reason):
    relay = FaultRelay([relay_status])
    client, _, adapter = make_client(relay)
    before = client.get("/api/exercises").json()
    started = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    result = wait_for_grade(client, started.json()["job_id"])
    assert started.status_code == 202
    assert result["status"] == "blocked" and expected_reason in result["reason"]
    assert "score" not in result and "points_charged" not in result
    assert adapter.write_calls == [] and client.get("/api/exercises").json() == before


def test_upstream_502_is_retryable_and_recovery_writes_exactly_once():
    relay = FaultRelay([502])
    client, _, adapter = make_client(relay)
    first = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    failed = wait_for_grade(client, first.json()["job_id"])
    assert failed["status"] == "failed" and failed["retryable"] is True
    assert "score" not in failed and "points_charged" not in failed and adapter.write_calls == []
    second = client.post(f"/api/exercises/{EXERCISE_GENERATED}/grade")
    completed = wait_for_grade(client, second.json()["job_id"])
    assert completed["status"] == "completed" and len(adapter.write_calls) == 1
    assert relay.calls == ["grade", "grade"]


def test_grading_ui_has_distinct_retryable_error_and_status_safe_copy():
    from pathlib import Path
    script = Path(__file__).parents[1].joinpath("pku_sync", "panel", "static", "app.js").read_text(encoding="utf-8")
    assert 'g.status === "failed"' in script
    assert "grading-retry" in script
