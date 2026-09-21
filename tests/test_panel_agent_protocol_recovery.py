from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pku_sync.agent_runner import AgentRun
from pku_sync.panel.exercise_organizer import MAX_AGENT_OUTPUT_CHARS, agent_dispatch_result, make_organizer_service
from pku_sync.panel.fake_panel import build_fake_panel_services
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L1
from pku_sync.panel.webapi import create_app

AGENT_CASES = {
    "organize-agent-missing": (
        "\u4ee3\u7406\u8fd4\u56de\u7f3a\u5c11 TUI_RESULT \u6807\u8bb0\u3002",
        "SAFE_DIAGNOSTIC <missing-marker> no final result",
    ),
    "organize-agent-blocked": (
        "\u4ee3\u7406\u660e\u786e\u963b\u6b62\u672c\u6b21\u6574\u7406\u3002",
        "SAFE_DIAGNOSTIC explicit blocked\n"
        "TUI_RESULT=blocked reason=\u4ee3\u7406\u660e\u786e\u963b\u6b62\u672c\u6b21\u6574\u7406\u3002",
    ),
    "organize-agent-mcp-down": (
        "\u7ec3\u4e60\u6574\u7406\u4ee3\u7406\u65e0\u6cd5\u8fde\u63a5 MCP\uff0c\u672a\u6267\u884c\u6574\u7406\u3002",
        "SAFE_DIAGNOSTIC MCP unavailable",
    ),
    "organize-agent-timeout": (
        "\u7ec3\u4e60\u6574\u7406\u4ee3\u7406\u7b49\u5f85\u8d85\u65f6\uff0c\u672a\u6267\u884c\u6574\u7406\u3002",
        "SAFE_DIAGNOSTIC timeout before completion",
    ),
    "organize-agent-multiple": (
        "\u4ee3\u7406\u8fd4\u56de\u4e86\u591a\u4e2a TUI_RESULT \u6807\u8bb0\u3002",
        "SAFE_DIAGNOSTIC conflicting markers\nTUI_RESULT=success\n"
        "TUI_RESULT=blocked reason=\u4ee3\u7406\u660e\u786e\u963b\u6b62\u672c\u6b21\u6574\u7406\u3002",
    ),
}

SUCCESS_FIELDS = {"exercise_id", "url", "title", "state", "points_charged", "points_remaining", "reused"}


def make_variant(variant: str):
    directory, connection, platform, organizer, grading = build_fake_panel_services(variant)
    app = create_app(directory_service=directory, connection_service=connection, platform_service=platform, organizer_service=organizer, grading_service=grading)
    return TestClient(app), platform, organizer


def organize(client: TestClient) -> dict:
    started = client.post("/api/exercises/organize", json={"course_id": COURSE_NET, "lecture_ids": [NET_L1]})
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    for _ in range(400):
        result = client.get("/api/exercises/organize/status", params={"job_id": job_id})
        if result.json().get("status") != "running":
            return result.json()
        time.sleep(0.005)
    pytest.fail("organize job did not finish")


@pytest.mark.parametrize("variant", AGENT_CASES)
def test_fake_agent_protocol_failures_keep_reason_and_controlled_output_separate(variant):
    client, platform, organizer = make_variant(variant)
    reason, output = AGENT_CASES[variant]
    before_rows = client.get("/api/exercises").json()["exercises"]
    before_balance = platform.quota()["llm_points_remaining"]
    blocked = organize(client)
    assert blocked == {"status": "blocked", "reason": reason, "retryable": True, "output": output}
    assert not (SUCCESS_FIELDS & set(blocked))
    assert platform.llm_calls == []
    assert organizer.page_adapter.create_calls == []
    assert organizer.record_store.records == {}
    assert client.get("/api/exercises").json()["exercises"] == before_rows
    assert platform.quota()["llm_points_remaining"] == before_balance


def test_agent_output_is_opt_in_bounded_and_never_uses_stderr_as_public_copy():
    secret = "secret_test_agent_output"
    raw = "x" * (MAX_AGENT_OUTPUT_CHARS + 25)
    run = AgentRun("factory", Path("synthetic-output"), 9, stderr=f"MCP failed with {secret} at E:\\private\\notes.md", output=raw)
    default = agent_dispatch_result(run)
    allowed = agent_dispatch_result(run, preserve_output=True)
    assert default.output == ""
    assert allowed.reason == "\u7ec3\u4e60\u6574\u7406\u4ee3\u7406\u672a\u5b8c\u6210\uff0c\u672a\u6267\u884c\u6574\u7406\u3002"
    assert secret not in allowed.reason and "private" not in allowed.reason
    assert secret not in allowed.output and "private" not in allowed.output
    assert allowed.output == raw[:MAX_AGENT_OUTPUT_CHARS]


def test_empty_agent_output_is_omitted_from_blocked_poll_payload():
    class EmptyOutputOrganizer:
        def estimate(self):
            return {"label": "estimate", "min_points": 1, "max_points": 5}
        def organize(self, course_id, lecture_ids):
            from pku_sync.panel.exercise_organizer import OrganizeBlocked
            raise OrganizeBlocked(400, "\u4ee3\u7406\u672a\u6267\u884c\u6574\u7406\u3002", output="")
    blocked = organize(TestClient(create_app(organizer_service=EmptyOutputOrganizer())))
    assert blocked == {"status": "blocked", "reason": "\u4ee3\u7406\u672a\u6267\u884c\u6574\u7406\u3002", "retryable": True}


def test_stateful_agent_recovery_charges_and_creates_once_then_reuses_identity():
    client, platform, organizer = make_variant("organize-agent-recovery")
    before_rows = client.get("/api/exercises").json()["exercises"]
    blocked = organize(client)
    assert blocked == {"status": "blocked", "reason": "\u4ee3\u7406\u8fd4\u56de\u7f3a\u5c11 TUI_RESULT \u6807\u8bb0\u3002", "retryable": True, "output": "SAFE_DIAGNOSTIC <missing-marker> first attempt"}
    assert len(organizer.agent_dispatcher.calls) == 1
    assert platform.llm_calls == [] and organizer.page_adapter.create_calls == [] and organizer.record_store.records == {}
    completed = organize(client)
    assert completed["status"] == "completed" and completed["reused"] is False and completed["points_charged"] == 3.0
    assert "output" not in completed
    assert len(platform.llm_calls) == len(organizer.page_adapter.create_calls) == len(organizer.record_store.records) == 1
    after_rows = client.get("/api/exercises").json()["exercises"]
    assert len(after_rows) == len(before_rows) + 1
    reused = organize(client)
    assert reused["exercise_id"] == completed["exercise_id"] and reused["reused"] is True and reused["points_charged"] == 0
    assert "output" not in reused
    assert len(organizer.agent_dispatcher.calls) == 2
    assert len(platform.llm_calls) == len(organizer.page_adapter.create_calls) == len(organizer.record_store.records) == 1
    assert client.get("/api/exercises").json()["exercises"] == after_rows


def test_normal_fake_and_production_organizers_remain_dispatcher_free(tmp_path):
    _client, _platform, fake_organizer = make_variant("normal")
    assert fake_organizer.agent_dispatcher is None and fake_organizer.preserve_agent_output is False
    class Settings:
        data_dir = tmp_path
        exercise_e2e_enabled = False
    production = make_organizer_service(Settings(), object(), object())
    assert production.agent_dispatcher is None and production.preserve_agent_output is False
