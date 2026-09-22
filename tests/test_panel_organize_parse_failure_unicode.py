"""Additive surrogate-safety coverage for the organizer parse-failure capture.

The bounded raw relay capture (``bound_relay_content`` /
``persist_parse_failure``) is the diagnostics leg of a charged organize
operation lost at the strict quiz gate. Scrutiny round 1 found the defect:
relay content holding an unpaired Unicode surrogate made ``bound_relay_content``
raise UnicodeEncodeError outside the best-effort persistence guard, so the
exact blocked reason ``AI 返回的练习格式不完整，请重试。`` was replaced by
the API's generic organizer error ``练习整理没有完成，请稍后重试。``.

These additive tests prove surrogate-bearing invalid quiz content still
yields the unchanged blocked reason, that a bounded read-only capture file
with replacement text is still written, that e2e_mode alone echoes bounded
``raw_content`` while normal-mode payloads stay byte-identical, and that no
exception escapes into the generic organizer error. All relays are
deterministic fakes — no real LLM, Notion, or other spend.
"""
from __future__ import annotations

import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from pku_sync.panel.exercise_organizer import (
    MAX_PARSE_FAILURE_CAPTURE_BYTES,
    ExerciseOrganizer,
    MemoryOrganizeRecordStore,
    OrganizeBlocked,
    bound_relay_content,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L1, build_panel_workspace
from pku_sync.panel.webapi import create_app

BLOCKED_REASON = "AI 返回的练习格式不完整，请重试。"

# Surrogate-bearing invalid quiz content: prose with NO recoverable JSON
# object, carrying unpaired Unicode surrogates (high and low).
SURROGATE_HIGH = "没有任何 JSON 对象的说明。\ud83d"
SURROGATE_LOW = "没有任何 JSON 对象的说明。\ude00"
SURROGATE_MIXED = "说明前缀 \ud83d \ude00 这里仍然没有任何 JSON 对象。"


class RawContentRelay:
    """Deterministic fake relay returning one exact raw content string."""

    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    def quota(self) -> dict:
        return {"available": True, "llm_points_remaining": 20.0}

    def quiz(self, _prompt: str) -> dict:
        self.calls += 1
        return {"content": self.content, "points_charged": 2.0}


class FakePages:
    def __init__(self):
        self.calls: list[dict] = []

    def create_page(self, parent_page_id: str, title: str, *, children: list[dict]):
        self.calls.append(
            {"parent_page_id": parent_page_id, "title": title, "children": children}
        )
        number = len(self.calls)
        return {
            "id": f"2c000001-0000-4000-8000-{number:012d}",
            "url": f"https://www.notion.so/surrogate-{number}",
            "last_edited_time": "2026-09-22T08:00:00.000Z",
        }


class FakeNotes:
    def for_scope(self, *, course_title: str, lecture_titles: list[str]) -> list[dict]:
        return [
            {
                "title": lecture_titles[0],
                "source": "课堂录像笔记",
                "notes": "分层与端到端。",
            }
        ]


def make_organizer(content: str, *, parse_failure_dir=None, e2e_enabled: bool = False):
    directory = build_fake_directory(workspace=build_panel_workspace())
    relay = RawContentRelay(content)
    pages = FakePages()
    organizer = ExerciseOrganizer(
        directory_service=directory,
        relay=relay,
        page_adapter=pages,
        notes_provider=FakeNotes(),
        record_store=MemoryOrganizeRecordStore(),
        parse_failure_dir=parse_failure_dir,
        e2e_enabled=e2e_enabled,
    )
    return organizer, relay, pages


def wait_for_organize_result(client: TestClient, job_id: str) -> dict:
    for _ in range(400):
        response = client.get(
            "/api/exercises/organize/status", params={"job_id": job_id}
        )
        payload = response.json()
        if payload.get("status") != "running":
            return payload
        time.sleep(0.005)
    raise AssertionError("organize job did not finish")


def capture_files(directory) -> list:
    return sorted(directory.glob("*.txt"))


def is_read_only(path) -> bool:
    return stat.S_IMODE(os.stat(path).st_mode) & 0o222 == 0


@pytest.mark.parametrize("content", [SURROGATE_HIGH, SURROGATE_LOW, SURROGATE_MIXED])
def test_surrogate_invalid_quiz_yields_the_normal_blocked_reason(tmp_path, content):
    failure_dir = tmp_path / "panel" / "organize_failures"
    organizer, relay, pages = make_organizer(content, parse_failure_dir=failure_dir)

    with pytest.raises(OrganizeBlocked) as raised:
        organizer.organize(COURSE_NET, [NET_L1])

    # The exact user-facing reason survives; the generic organizer error
    # would prove the capture raised and escaped the guarded diagnostics leg.
    assert raised.value.status_code == 502
    assert raised.value.reason == BLOCKED_REASON
    assert relay.calls == 1 and pages.calls == []


@pytest.mark.parametrize("content", [SURROGATE_HIGH, SURROGATE_LOW, SURROGATE_MIXED])
def test_surrogate_failure_writes_bounded_read_only_capture_with_replacement_text(
    tmp_path, content
):
    failure_dir = tmp_path / "organize_failures"
    organizer, relay, pages = make_organizer(content, parse_failure_dir=failure_dir)

    with pytest.raises(OrganizeBlocked, match=BLOCKED_REASON):
        organizer.organize(COURSE_NET, [NET_L1])

    files = capture_files(failure_dir)
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    # Replacement text stands where the unpaired surrogates were (the raw
    # capture text is UTF-8-safe and bounded).
    assert "?" in text or "\ufffd" in text
    assert len(text.encode("utf-8")) <= MAX_PARSE_FAILURE_CAPTURE_BYTES
    assert is_read_only(files[0])
    assert relay.calls == 1 and pages.calls == []


@pytest.mark.parametrize("content", [SURROGATE_HIGH, SURROGATE_MIXED])
def test_e2e_mode_only_echo_attaches_bounded_surrogate_safe_raw_content(
    tmp_path, content
):
    organizer, _relay, _pages = make_organizer(
        content, parse_failure_dir=tmp_path / "failures", e2e_enabled=True
    )

    with pytest.raises(OrganizeBlocked) as raised:
        organizer.organize(COURSE_NET, [NET_L1], e2e_mode=True)

    assert raised.value.reason == BLOCKED_REASON
    assert "?" in raised.value.raw_content or "\ufffd" in raised.value.raw_content
    assert len(raised.value.raw_content.encode("utf-8")) <= MAX_PARSE_FAILURE_CAPTURE_BYTES

    with pytest.raises(OrganizeBlocked) as raised_normal:
        organizer.organize(COURSE_NET, [NET_L1])
    assert raised_normal.value.reason == BLOCKED_REASON
    assert raised_normal.value.raw_content == ""


@pytest.mark.parametrize("content", [SURROGATE_HIGH, SURROGATE_LOW])
def test_api_surrogate_failure_keeps_normal_payload_byte_identical(tmp_path, content):
    organizer, _relay, _pages = make_organizer(
        content, parse_failure_dir=tmp_path / "failures", e2e_enabled=True
    )
    client = TestClient(create_app(organizer_service=organizer))

    started = client.post(
        "/api/exercises/organize",
        json={"course_id": COURSE_NET, "lecture_ids": [NET_L1]},
    )
    assert started.status_code == 202
    result = wait_for_organize_result(client, started.json()["job_id"])

    # Normal-mode blocked payloads stay byte-identical to today: reason,
    # retryable, and nothing else — no generic organizer error, no raw echo.
    assert result == {
        "status": "blocked",
        "reason": BLOCKED_REASON,
        "retryable": True,
    }


def test_api_surrogate_failure_echoes_bounded_raw_content_only_in_e2e_mode(tmp_path):
    organizer, _relay, _pages = make_organizer(
        SURROGATE_MIXED, parse_failure_dir=tmp_path / "failures", e2e_enabled=True
    )
    client = TestClient(create_app(organizer_service=organizer))

    started = client.post(
        "/api/exercises/organize",
        json={"course_id": COURSE_NET, "lecture_ids": [NET_L1], "e2e_mode": True},
    )
    assert started.status_code == 202
    result = wait_for_organize_result(client, started.json()["job_id"])

    assert result["status"] == "blocked"
    assert result["reason"] == BLOCKED_REASON
    assert "?" in result["raw_content"] or "\ufffd" in result["raw_content"]
    assert len(result["raw_content"].encode("utf-8")) <= MAX_PARSE_FAILURE_CAPTURE_BYTES


@pytest.mark.parametrize("content", [SURROGATE_HIGH, SURROGATE_LOW, SURROGATE_MIXED])
def test_bound_relay_content_never_raises_and_stays_bounded_on_surrogates(content):
    value = bound_relay_content(content)

    assert isinstance(value, str)
    assert len(value.encode("utf-8")) <= MAX_PARSE_FAILURE_CAPTURE_BYTES
    assert "?" in value or "\ufffd" in value


def test_bound_relay_content_rejects_non_string_inputs():
    for content in (None, 123, [SURROGATE_HIGH], {"raw": SURROGATE_HIGH}):
        assert bound_relay_content(content) == ""
