"""Bailian qwen thinking-mode control and reasoning-content normalization.

Pins the Window D first-run fix (m4-fix-relay-bailian-json-normalization):
the real qwen3.5-flash quiz call reported 612 input / 3811 output tokens
(78 millipoints) for a ~400-token JSON answer — the signature of qwen
thinking mode — and the relay passed ``message.content`` through raw, after
which the client's strict JSON gate rejected it post-charge
(library/window-d-first-run-findings.md). The adapter must (a) disable
thinking up front for qwen3 hybrid models (``enable_thinking: false`` on
the DashScope-compatible /chat/completions request) so the metered output
is the answer itself, and (b) normalize a residual reasoning wrapper
(thinking preamble / ```json fence / reasoning_content) to exactly one
clean JSON object for JSON-contract operations (quiz/grade), while plain
JSON and prose operations pass through unchanged. Fake transport only —
no real LLM spend.
"""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from server import accounts, relay
from server.llm import BailianLlm

QUIZ_JSON = json.dumps(
    {
        "title": "计算机网络基础练习",
        "questions": [
            {
                "type": "选择",
                "question": "OSI 参考模型共分为多少层？",
                "source": "第一讲",
                "answer": "7",
            }
        ],
    },
    ensure_ascii=False,
)

GRADE_JSON = json.dumps(
    {
        "contract_version": "pku-e2e-grade-v1",
        "score": 88,
        "earned_points": 5,
        "questions": [
            {
                "number": 1,
                "type": "选择",
                "score": 2,
                "max_score": 2,
                "correct": True,
                "register_wrong": False,
            }
        ],
    },
    ensure_ascii=False,
)


def thinking_shaped_quiz_content() -> str:
    """The shape the real qwen3.5-flash returned with thinking mode on:
    a reasoning preamble, then a fenced JSON, then closing prose."""
    return (
        "让我先思考题目要求：需要生成包含五种题型的练习，并在最后给出 JSON。\n"
        "```json\n"
        f"{QUIZ_JSON}\n"
        "```\n"
        "（以上即最终结果）"
    )


class RecordingTransport:
    """Fake /chat/completions transport: records the request body and
    returns a fixed vendor response. No network, no real spend."""

    def __init__(self, message=None, usage=None):
        self.calls = []
        self.message = message if message is not None else {"content": "{}"}
        self.usage = usage or {"prompt_tokens": 612, "completion_tokens": 3811}

    def __call__(self, url, headers, json, timeout):
        self.calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": self.message}], "usage": self.usage},
        )


def make_relay_client(tmp_path, llm_fn):
    """One app on a fresh scratch DB with one provisioned account."""
    conn = accounts.connect(tmp_path / "accounts.db")
    account_id = accounts.create_account(conn, "同学A")
    conn.commit()
    token = accounts.issue_session(conn, account_id)
    conn.close()
    app = relay.create_relay_app(tmp_path / "accounts.db", llm_fn=llm_fn)
    return TestClient(app), token


def llm_usage_rows(tmp_path):
    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        return conn.execute(
            "SELECT operation, model, input_tokens, output_tokens, millipoints"
            " FROM llm_usage ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


# --- thinking-mode control in the prepared /chat/completions request ---


def test_qwen3_request_disables_thinking_mode():
    transport = RecordingTransport()
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    llm.complete("quiz", "系统", "题目")
    body = transport.calls[0]["json"]
    assert body["model"] == "qwen3.5-flash"
    # qwen3 hybrid models think by default; the relay must switch it off so
    # the metered output is the answer itself.
    assert body["enable_thinking"] is False


def test_unknown_model_is_rejected_without_a_vendor_call():
    from server.llm import LlmError
    import pytest
    transport = RecordingTransport()
    llm = BailianLlm("key", "https://bailian.example/v1", model="qwen9-omni", post=transport)
    with pytest.raises(LlmError, match="unsupported AI model"):
        llm.complete("quiz", "系统", "题目")
    assert transport.calls == []


def test_real_run_shaped_quiz_response_is_normalized_to_one_json_object():
    transport = RecordingTransport(
        message={
            "role": "assistant",
            "content": thinking_shaped_quiz_content(),
            "reasoning_content": "先分析出题要求……（不应被当作返回内容）",
        },
        usage={"prompt_tokens": 612, "completion_tokens": 3811},
    )
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    result = llm.complete("quiz", "系统", "题目")
    # The client sees exactly one clean JSON object, not the preamble/fence.
    assert result.content == QUIZ_JSON
    assert json.loads(result.content)["questions"][0]["type"] == "选择"
    # Metering still reflects the real vendor usage; normalization does not
    # change the charge (612*.2 + 3811*2.0 per M = 0.0077444 RMB -> 78 mp).
    assert (result.input_tokens, result.output_tokens) == (612, 3811)
    assert result.millipoints == 78


def test_fenced_json_without_preamble_is_normalized():
    transport = RecordingTransport(message={"content": f"```json\n{QUIZ_JSON}\n```"})
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    result = llm.complete("quiz", "系统", "题目")
    assert result.content == QUIZ_JSON
    assert "```" not in result.content


def test_reasoning_content_field_is_not_served_as_the_answer():
    # The thinking phase lives in reasoning_content; complete() reads content
    # only, so the reasoning trace never reaches the client.
    transport = RecordingTransport(
        message={
            "role": "assistant",
            "content": GRADE_JSON,
            "reasoning_content": "一串不该返回的思考过程……",
        }
    )
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    result = llm.complete("grade", "系统", "批改题目")
    assert result.content == GRADE_JSON
    assert "思考" not in result.content


# --- pass-through: already-plain content is unchanged ---


def test_plain_json_content_passes_through_unchanged():
    transport = RecordingTransport(message={"content": QUIZ_JSON})
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    result = llm.complete("quiz", "系统", "题目")
    assert result.content == QUIZ_JSON


def test_plain_grade_json_content_passes_through_unchanged():
    transport = RecordingTransport(message={"content": GRADE_JSON})
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    result = llm.complete("grade", "系统", "批改题目")
    assert result.content == GRADE_JSON


def test_prose_operations_are_not_json_extracted():
    # notes/summary/qa return prose; braces in prose must never be mangled
    # by the JSON normalization.
    prose = (
        "本讲要点：协议分层 { 明确职责 } ，以及端到端传输。\n"
        "```text\n示例代码\n```"
    )
    transport = RecordingTransport(message={"content": prose})
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    assert llm.complete("summary", "系统", "内容").content == prose


def test_no_json_object_found_passes_through_unchanged():
    # A quiz/grade answer with no object at all is not fabricated: it passes
    # through so the client reports the failure honestly.
    transport = RecordingTransport(
        message={"content": "模型没有输出 JSON，只有一段文字。"}
    )
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    result = llm.complete("quiz", "系统", "题目")
    assert result.content == "模型没有输出 JSON，只有一段文字。"


# --- relay-level: the normalized content and honest settlement reach /v1/llm ---


def test_relay_surfaces_normalized_quiz_json_and_honest_settlement(tmp_path):
    transport = RecordingTransport(
        message={
            "role": "assistant",
            "content": thinking_shaped_quiz_content(),
            "reasoning_content": "思考……",
        },
        usage={"prompt_tokens": 612, "completion_tokens": 3811},
    )
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    client, token = make_relay_client(tmp_path, llm.complete)
    response = client.post(
        "/v1/llm",
        headers={"Authorization": f"Bearer {token}"},
        json={"operation": "quiz", "system": "系统", "user": "题目"},
    )
    assert response.status_code == 200
    payload = response.json()
    # The client-facing content is exactly one clean JSON object.
    assert payload["content"] == QUIZ_JSON
    assert payload["points_charged"] == 0.078  # 78 millipoints / 1000

    # The usage row records the REAL vendor usage, not the normalized text.
    rows = llm_usage_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["operation"] == "quiz"
    assert rows[0]["model"] == "qwen3.5-flash"
    assert rows[0]["input_tokens"] == 612
    assert rows[0]["output_tokens"] == 3811
    assert rows[0]["millipoints"] == 78
