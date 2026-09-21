"""Additive fence/prose-tolerance coverage for the organizer's strict quiz gate.

One stray Markdown code fence around an otherwise contract-valid quiz must
not burn the already-metered organize operation (the Window D first real run
rejected a real Bailian quiz response after the relay charge). Exactly one
fenced wrapper is unwrapped and accepted; a second fence, a nested fence, a
malformed payload, or contract-invalid content keeps the strict rejection
with the unchanged reasons. The shared bounded recovery layer
(``extract_single_json_payload``) additionally tolerates bounded non-object
text around EXACTLY ONE JSON object — leading prose and trailing commentary,
raw or fenced, CRLF variants — before the metered operation is declared lost
(the Window C first run burned a charged quiz op at this branch). Multiple
objects (e.g. a stray embedded example object), nested or unterminated
fences, non-json fence tags, prose with no object, non-object JSON top
levels, and over-bound wrappers stay rejected with today's reasons. A quiz
parse failure also persists the bounded raw relay content to a read-only
evidence file under the organize_failures directory and echoes it in the
blocked payload only in e2e_mode. All relays are deterministic fakes — no
real LLM, Notion, or other spend.
"""
from __future__ import annotations

import json
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from pku_sync.panel.exercise_organizer import (
    ExerciseOrganizer,
    MemoryOrganizeRecordStore,
    OrganizeBlocked,
    QUESTION_TYPES,
)
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.fake_workspace import COURSE_NET, NET_L1, build_panel_workspace
from pku_sync.panel.webapi import create_app


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
            "url": f"https://www.notion.so/fence-{number}",
            "last_edited_time": "2026-09-20T08:00:00.000Z",
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


def valid_quiz_payload() -> str:
    questions = [
        {
            "type": kind,
            "question": f"{kind}题",
            "source": "第一讲",
            "answer": f"{kind}答案",
        }
        for kind in sorted(QUESTION_TYPES)
    ]
    return json.dumps(
        {"title": "第一讲 · 计算机网络练习", "questions": questions}, ensure_ascii=False
    )


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


@pytest.mark.parametrize(
    "wrapper",
    [
        "```json\n{payload}\n```",
        "```\n{payload}\n```",
        "```JSON\n{payload}\n```",
        "``` json\n{payload}\n```",
        "~~~json\n{payload}\n~~~",
        "````json\n{payload}\n````",
        "\n```json\n{payload}\n```\n",
        "```json\r\n{payload}\r\n```",
        "```json\n{payload}\n```   \n",
    ],
)
def test_exactly_one_fence_wrapper_is_unwrapped_and_accepted(wrapper):
    organizer, relay, pages = make_organizer(wrapper.replace("{payload}", valid_quiz_payload()))

    result = organizer.organize(COURSE_NET, [NET_L1])

    assert result["status"] == "completed"
    assert result["title"] == "第一讲 · 计算机网络练习"
    assert result["points_charged"] == 2.0 and result["reused"] is False
    assert relay.calls == 1 and len(pages.calls) == 1
    assert pages.calls[0]["title"] == "第一讲 · 计算机网络练习"
    assert pages.calls[0]["children"]
    # The stored identity makes the retry a no-op: no second charge or create.
    retry = organizer.organize(COURSE_NET, [NET_L1])
    assert retry["reused"] is True and retry["points_charged"] == 0
    assert relay.calls == 1 and len(pages.calls) == 1


def test_plain_unfenced_json_keeps_working_unchanged():
    organizer, relay, pages = make_organizer(valid_quiz_payload())

    result = organizer.organize(COURSE_NET, [NET_L1])

    assert result["status"] == "completed" and result["reused"] is False
    assert relay.calls == 1 and len(pages.calls) == 1


def test_second_fence_is_rejected_without_page_or_identity_record():
    payload = valid_quiz_payload()
    organizer, relay, pages = make_organizer(f"```json\n{payload}\n```\n```json\n{payload}\n```")

    with pytest.raises(OrganizeBlocked, match="AI 返回的练习格式不完整，请重试。"):
        organizer.organize(COURSE_NET, [NET_L1])

    assert relay.calls == 1 and pages.calls == []
    # No record or registration survived the rejection: a good retry is fresh.
    relay.content = payload
    retried = organizer.organize(COURSE_NET, [NET_L1])
    assert retried["reused"] is False and len(pages.calls) == 1


def test_nested_fence_is_rejected_without_page_creation():
    payload = valid_quiz_payload()
    organizer, relay, pages = make_organizer(f"```json\n```json\n{payload}\n```\n```")

    with pytest.raises(OrganizeBlocked, match="AI 返回的练习格式不完整，请重试。"):
        organizer.organize(COURSE_NET, [NET_L1])

    assert relay.calls == 1 and pages.calls == []


def test_malformed_json_inside_single_fence_is_rejected_without_page_creation():
    organizer, relay, pages = make_organizer(
        '```json\n{"title": "第一讲练习", "questions": [\n```'
    )

    with pytest.raises(OrganizeBlocked, match="AI 返回的练习格式不完整，请重试。"):
        organizer.organize(COURSE_NET, [NET_L1])

    assert relay.calls == 1 and pages.calls == []


def _quiz_mutations():
    """Return the contract-invalid quiz shapes with today's blocking reasons."""

    def drop_question(payload):
        payload["questions"].pop()
        return payload

    def duplicate_type(payload):
        payload["questions"][1]["type"] = "选择"
        return payload

    def unknown_type(payload):
        payload["questions"][0]["type"] = "单选"
        return payload

    def empty_answer(payload):
        payload["questions"][2]["answer"] = " "
        return payload

    def questions_not_a_list(payload):
        payload["questions"] = {"选择": "一道"}
        return payload

    def top_level_not_a_dict(payload):
        return [payload]

    def question_item_not_a_dict(payload):
        payload["questions"][3] = "第四题"
        return payload

    def missing_source(payload):
        del payload["questions"][4]["source"]
        return payload

    return {
        "missing-question": drop_question,
        "duplicate-type": duplicate_type,
        "unknown-type": unknown_type,
        "empty-answer": empty_answer,
        "questions-not-list": questions_not_a_list,
        "top-level-not-dict": top_level_not_a_dict,
        "item-not-dict": question_item_not_a_dict,
        "missing-source": missing_source,
    }


@pytest.mark.parametrize("name", list(_quiz_mutations()))
def test_contract_invalid_content_inside_fence_is_rejected_with_unchanged_reasons(name):
    payload = _quiz_mutations()[name](json.loads(valid_quiz_payload()))
    organizer, relay, pages = make_organizer(
        f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"
    )

    with pytest.raises(OrganizeBlocked) as raised:
        organizer.organize(COURSE_NET, [NET_L1])

    assert raised.value.status_code == 502
    assert raised.value.reason in {
        "AI 返回的练习格式不完整，请重试。",
        "AI 返回的题目数量不符合要求，请重试。",
        "AI 返回的题型不够丰富，请重试。",
    }
    assert relay.calls == 1 and pages.calls == []


@pytest.mark.parametrize(
    "content",
    [
        # The two prose-wrapped shapes from the original list are now
        # recovered by the shared bounded prose layer (tests below); the
        # remaining rejection entries are byte-identical to before.
        "```json\n{payload}",
        "```python\n{payload}\n```",
        "```json pretty\n{payload}\n```",
    ],
)
def test_loose_wrapper_shapes_stay_rejected_exactly_as_today(content):
    organizer, relay, pages = make_organizer(content.replace("{payload}", valid_quiz_payload()))

    with pytest.raises(OrganizeBlocked, match="AI 返回的练习格式不完整，请重试。"):
        organizer.organize(COURSE_NET, [NET_L1])

    assert relay.calls == 1 and pages.calls == []


@pytest.mark.parametrize(
    "content",
    [
        "好的，以下是整理好的练习：\n{payload}",
        "{payload}\n以上是全部练习，希望对你有帮助。",
        "这是练习：\n{payload}\n以上是全部练习。",
        "练习如下：\r\n{payload}\r\n请查收。",
    ],
)
def test_prose_around_one_raw_object_is_recovered_and_accepted(content):
    organizer, relay, pages = make_organizer(content.replace("{payload}", valid_quiz_payload()))

    result = organizer.organize(COURSE_NET, [NET_L1])

    assert result["status"] == "completed" and result["reused"] is False
    assert result["title"] == "第一讲 · 计算机网络练习"
    assert result["points_charged"] == 2.0
    assert relay.calls == 1 and len(pages.calls) == 1
    assert pages.calls[0]["children"]
    # The stored identity makes the retry a no-op: no second charge or create.
    retry = organizer.organize(COURSE_NET, [NET_L1])
    assert retry["reused"] is True and retry["points_charged"] == 0
    assert relay.calls == 1 and len(pages.calls) == 1


@pytest.mark.parametrize(
    "content",
    [
        "这是练习：\n```json\n{payload}\n```",
        "```json\n{payload}\n```\n以上是全部练习。",
        "这是练习：\n```json\n{payload}\n```\n以上是全部练习。",
        "这是练习：\r\n```json\r\n{payload}\r\n```\r\n以上是全部练习。",
    ],
)
def test_prose_around_one_fenced_object_is_recovered_and_accepted(content):
    organizer, relay, pages = make_organizer(content.replace("{payload}", valid_quiz_payload()))

    result = organizer.organize(COURSE_NET, [NET_L1])

    assert result["status"] == "completed" and result["reused"] is False
    assert result["title"] == "第一讲 · 计算机网络练习"
    assert result["points_charged"] == 2.0
    assert relay.calls == 1 and len(pages.calls) == 1
    retry = organizer.organize(COURSE_NET, [NET_L1])
    assert retry["reused"] is True and relay.calls == 1 and len(pages.calls) == 1


@pytest.mark.parametrize(
    "content",
    [
        "示例：{\"example\": true}\n\n正式练习：{payload}",
        "这是练习：{payload}，再来一份：{payload}",
        "示例：{\"example\": true}\n\n```json\n{payload}\n```",
        "```json\n{payload}\n```\n附一个示例对象：{\"example\": true}",
    ],
)
def test_second_or_stray_example_object_is_rejected_without_page_creation(content):
    organizer, relay, pages = make_organizer(content.replace("{payload}", valid_quiz_payload()))

    with pytest.raises(OrganizeBlocked, match="AI 返回的练习格式不完整，请重试。"):
        organizer.organize(COURSE_NET, [NET_L1])

    assert relay.calls == 1 and pages.calls == []


@pytest.mark.parametrize(
    "content",
    [
        "[\"选择\", \"判断\"]",
        "以下是题目列表：\n[\"选择\", \"判断\"]\n以上。",
        "练习稍后奉上，先看这段说明文字。",
        "\"练习稍后奉上\"",
    ],
)
def test_non_object_or_object_less_content_is_rejected_without_page_creation(content):
    organizer, relay, pages = make_organizer(content)

    with pytest.raises(OrganizeBlocked, match="AI 返回的练习格式不完整，请重试。"):
        organizer.organize(COURSE_NET, [NET_L1])

    assert relay.calls == 1 and pages.calls == []


def test_over_bound_prose_around_one_object_is_rejected_without_page_creation():
    payload = valid_quiz_payload()
    content = "啰嗦说明。" * 2000 + "\n" + payload

    organizer, relay, pages = make_organizer(content)

    with pytest.raises(OrganizeBlocked, match="AI 返回的练习格式不完整，请重试。"):
        organizer.organize(COURSE_NET, [NET_L1])

    assert relay.calls == 1 and pages.calls == []


def test_parse_failure_persists_bounded_read_only_raw_content(tmp_path):
    failure_dir = tmp_path / "panel" / "organize_failures"
    raw = "这是整理说明，但里面没有任何 JSON 对象。"
    organizer, relay, pages = make_organizer(raw, parse_failure_dir=failure_dir)

    with pytest.raises(OrganizeBlocked, match="AI 返回的练习格式不完整，请重试。"):
        organizer.organize(COURSE_NET, [NET_L1])

    files = sorted(failure_dir.glob("*.txt"))
    assert len(files) == 1
    # The capture holds exactly the bounded raw relay content — the only
    # writer input is the metered relay response, so no credentials can
    # appear by construction.
    assert files[0].read_text(encoding="utf-8") == raw
    assert stat.S_IMODE(os.stat(files[0]).st_mode) & 0o222 == 0
    assert relay.calls == 1 and pages.calls == []


def test_parse_failure_capture_is_bounded_to_the_first_eight_kib(tmp_path):
    failure_dir = tmp_path / "organize_failures"
    raw = "没有任何对象的超长说明。" * 2000

    organizer, _relay, _pages = make_organizer(raw, parse_failure_dir=failure_dir)

    with pytest.raises(OrganizeBlocked, match="AI 返回的练习格式不完整，请重试。"):
        organizer.organize(COURSE_NET, [NET_L1])

    files = sorted(failure_dir.glob("*.txt"))
    assert len(files) == 1
    data = files[0].read_bytes()
    assert len(data) <= 8192
    assert raw.encode("utf-8").startswith(data)
    data.decode("utf-8")  # never a torn codepoint


def test_blocked_parse_failure_attaches_bounded_raw_content_only_in_e2e_mode(tmp_path):
    raw = "整理说明：这里没有任何 JSON 对象。"
    organizer, _relay, _pages = make_organizer(
        raw, parse_failure_dir=tmp_path / "failures", e2e_enabled=True
    )

    with pytest.raises(OrganizeBlocked) as raised:
        organizer.organize(COURSE_NET, [NET_L1], e2e_mode=True)

    assert raised.value.raw_content == raw

    with pytest.raises(OrganizeBlocked) as raised_normal:
        organizer.organize(COURSE_NET, [NET_L1])

    assert raised_normal.value.raw_content == ""


def test_api_blocked_payload_echoes_raw_content_only_in_e2e_mode(tmp_path):
    raw = "整理说明：这里没有任何 JSON 对象。"
    organizer, _relay, _pages = make_organizer(
        raw, parse_failure_dir=tmp_path / "failures", e2e_enabled=True
    )
    client = TestClient(create_app(organizer_service=organizer))

    started = client.post(
        "/api/exercises/organize",
        json={"course_id": COURSE_NET, "lecture_ids": [NET_L1], "e2e_mode": True},
    )
    assert started.status_code == 202
    result = wait_for_organize_result(client, started.json()["job_id"])
    assert result["status"] == "blocked"
    assert result["reason"] == "AI 返回的练习格式不完整，请重试。"
    assert result["raw_content"] == raw

    started = client.post(
        "/api/exercises/organize",
        json={"course_id": COURSE_NET, "lecture_ids": [NET_L1]},
    )
    result = wait_for_organize_result(client, started.json()["job_id"])
    # Normal-mode payloads stay byte-identical to today: reason, retryable,
    # and nothing else.
    assert result == {
        "status": "blocked",
        "reason": "AI 返回的练习格式不完整，请重试。",
        "retryable": True,
    }
