"""Bailian JSON response mode plus finish/wrapper honesty on the metered path.

Pins the Window C pre-retry fix (m4-fix-relay-llm-json-response-mode): the
first real Window C run charged one quiz op (qwen3.5-flash, 11 millipoints)
whose content the client's strict json.loads gate rejected post-charge, and
no surface retained the raw content. The relay now (a) asks Bailian for
structured JSON output on the JSON-contract operations (quiz/grade):
``response_format={"type": "json_object"}`` as a top-level /chat/completions
body field — qwen3.5-flash is json_object-eligible in the non-thinking mode
the relay already forces, the fixed client prompts already carry the literal
JSON keyword, and json_object constrains the vendor to valid JSON (but NOT
to the quiz/grade schema, so the client's shape validation stays
authoritative); (b) fails LOUD pre-settlement when the vendor reports a
finish_reason other than "stop" on a JSON-contract op — a length-truncated
object would otherwise be served as content and only fail client-side after
the burn; and (c) strips reasoning/wrapper text only when the extracted
balanced object is the ONLY one in the content, so an embedded example
object in prose can never replace the real result. Prose operations (notes,
summary, qa) keep byte-identical payloads. ``enable_thinking: false`` and
the wrapper normalization stay as defense-in-depth; metering is unchanged.
Fake transport only — no real LLM spend.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from server import accounts, relay
from server.llm import BailianLlm, LlmError

QUIZ_JSON = json.dumps(
    {
        "title": "计算机网络基础练习",
        "questions": [
            {
                "type": "选择",
                "question": "OSI 参考模型共分为多少层？",
                "source": "第一讲",
                "answer": "7",
            },
            {
                "type": "填空",
                "question": "TCP 工作在传输层，负责端到端的____传输。",
                "source": "第一讲",
                "answer": "可靠",
            },
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

# The Window C first-run shape: qwen3.5-flash at ¥0.2/M in, ¥2.0/M out on
# 612 input / 449 output tokens → ¥0.0010204 → ceil × 10 000 = 11
# millipoints — the charge the client could not parse.
WINDOW_C_USAGE = {"prompt_tokens": 612, "completion_tokens": 449}
WINDOW_C_MILLIPOINTS = 11


def thinking_shaped_quiz_content() -> str:
    """A residual reasoning wrapper around exactly ONE JSON object."""
    return (
        "让我先思考题目要求，再按要求输出 JSON。\n"
        "```json\n"
        f"{QUIZ_JSON}\n"
        "```\n"
        "（以上即最终结果）"
    )


def two_object_wrapper_content() -> str:
    """Wrapper content with an EXAMPLE object embedded in prose BEFORE the
    real answer: the first balanced parseable object is the example, so the
    old first-object extraction would have served the example as the quiz."""
    example = json.dumps({"title": "示例格式", "questions": []}, ensure_ascii=False)
    return (
        "先给出示例格式，然后输出正式结果 JSON。\n"
        f"示例：{example}\n"
        "```json\n"
        f"{QUIZ_JSON}\n"
        "```\n"
        "（以上为正式结果）"
    )


class RecordingTransport:
    """Fake /chat/completions transport: records the request body and
    returns a fixed vendor response, finish_reason included (the real
    OpenAI-compatible endpoint always reports it). No network, no spend."""

    def __init__(self, message=None, usage=None, finish_reason="stop", status_code=200):
        self.calls = []
        self.message = message if message is not None else {"content": "{}"}
        self.usage = usage or dict(WINDOW_C_USAGE)
        self.finish_reason = finish_reason
        self.status_code = status_code

    def __call__(self, url, headers, json, timeout):
        self.calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        if self.status_code != 200:
            # A request-time vendor rejection (e.g. a model family that does
            # not accept response_format); the body must never be echoed.
            return httpx.Response(
                self.status_code, json={"error": {"message": "rejected"}}
            )
        return httpx.Response(
            self.status_code,
            json={
                "choices": [
                    {"message": self.message, "finish_reason": self.finish_reason}
                ],
                "usage": self.usage,
            },
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


def llm_balance(tmp_path):
    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        return conn.execute(
            "SELECT llm_millipoints_remaining FROM accounts WHERE label = '同学A'"
        ).fetchone()[0]
    finally:
        conn.close()


def llm_usage_rows(tmp_path):
    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        return conn.execute(
            "SELECT operation, model, input_tokens, output_tokens, millipoints"
            " FROM llm_usage ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def llm_request(client, token, operation="quiz", system="只返回 JSON。", user="题目"):
    return client.post(
        "/v1/llm",
        headers={"Authorization": f"Bearer {token}"},
        json={"operation": operation, "system": system, "user": user},
    )


# --- JSON response mode in the prepared /chat/completions request ---


def test_quiz_payload_carries_json_object_response_mode_and_thinking_off():
    transport = RecordingTransport(message={"content": QUIZ_JSON})
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    llm.complete("quiz", "只返回 JSON。", "题目")
    body = transport.calls[0]["json"]
    assert body["response_format"] == {"type": "json_object"}
    # json_object is eligible for qwen3.5-flash only in the non-thinking
    # mode the relay already forces.
    assert body["enable_thinking"] is False


def test_grade_payload_carries_json_object_response_mode_and_thinking_off():
    transport = RecordingTransport(message={"content": GRADE_JSON})
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    llm.complete("grade", "只返回 JSON。", "批改题目")
    body = transport.calls[0]["json"]
    assert body["response_format"] == {"type": "json_object"}
    assert body["enable_thinking"] is False


def test_prose_operation_payloads_stay_byte_identical_without_response_format():
    # notes/summary/qa must not grow the response_format field; their
    # payloads stay byte-identical to today (thinking control included,
    # exactly as the thinking-mode fix left them).
    for operation in ("notes", "summary", "qa"):
        transport = RecordingTransport(message={"content": "一段说明文字。"})
        llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
        llm.complete(operation, "系统提示", "内容")
        body = transport.calls[0]["json"]
        assert body == {
            "model": "qwen3.5-flash",
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "内容"},
            ],
            "enable_thinking": False,
        }, operation
        assert "response_format" not in body


def test_unknown_model_is_rejected_without_a_vendor_call():
    from server.llm import LlmError
    import pytest
    transport = RecordingTransport()
    llm = BailianLlm("key", "https://bailian.example/v1", model="qwen9-omni", post=transport)
    with pytest.raises(LlmError, match="unsupported AI model"):
        llm.complete("quiz", "系统", "题目")
    assert transport.calls == []


def test_vendor_rejection_of_response_format_is_502_pre_charge(tmp_path):
    # A vendor request-time rejection of the parameter surfaces as today's
    # generic 502 BEFORE settlement: no usage row, no burn to the user.
    transport = RecordingTransport(status_code=400)
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    client, token = make_relay_client(tmp_path, llm.complete)
    before = llm_balance(tmp_path)
    response = llm_request(client, token)
    assert response.status_code == 502
    assert response.json()["detail"] == "AI operation failed upstream"
    assert "rejected" not in response.text  # the upstream body stays private
    assert llm_balance(tmp_path) == before
    assert llm_usage_rows(tmp_path) == []


# --- finish_reason honesty on JSON-contract operations ---


@pytest.mark.parametrize("operation", ["quiz", "grade"])
def test_non_stop_finish_reason_raises_llm_error(operation):
    # A JSON-contract answer that did not finish ("length" truncation, a
    # content filter, …) is an LlmError, not served content: it would only
    # fail the client's strict JSON gate after the charge.
    transport = RecordingTransport(
        message={"content": QUIZ_JSON}, finish_reason="length"
    )
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    with pytest.raises(LlmError):
        llm.complete(operation, "只返回 JSON。", "题目")


def test_length_truncated_quiz_is_502_with_no_settlement(tmp_path):
    # The loud pre-settlement leg: the truncated content never reaches the
    # client, nothing is charged, and no metering row is written.
    truncated_quiz = QUIZ_JSON[:-12]  # cut mid-tail: invalid JSON
    transport = RecordingTransport(
        message={"content": truncated_quiz}, finish_reason="length"
    )
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    client, token = make_relay_client(tmp_path, llm.complete)
    before = llm_balance(tmp_path)
    response = llm_request(client, token)
    assert response.status_code == 502
    assert response.json()["detail"] == "AI operation failed upstream"
    assert truncated_quiz not in response.text  # truncated content is not served
    assert llm_balance(tmp_path) == before
    assert llm_usage_rows(tmp_path) == []
    assert len(transport.calls) == 1  # one vendor call, zero settlement


def test_length_finish_reason_prose_operation_still_completes():
    # The loud finish check is scoped to JSON-contract ops: truncated prose
    # is still served as prose, exactly as today (no strict gate rejects it).
    transport = RecordingTransport(
        message={"content": "一段被截断的说明文字"}, finish_reason="length"
    )
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    result = llm.complete("summary", "系统", "内容")
    assert result.content == "一段被截断的说明文字"


def test_absent_finish_reason_is_not_treated_as_truncation():
    # Absence is not proof of truncation (and the vendor shape before this
    # fix, plus older fixtures, omit the field): the call completes and the
    # client's shape validation stays authoritative.
    transport = RecordingTransport(message={"content": QUIZ_JSON}, finish_reason=None)
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    result = llm.complete("quiz", "只返回 JSON。", "题目")
    assert result.content == QUIZ_JSON


# --- wrapper extraction strips only when the object is the ONLY one ---


def test_single_object_wrapper_is_still_normalized():
    transport = RecordingTransport(message={"content": thinking_shaped_quiz_content()})
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    result = llm.complete("quiz", "只返回 JSON。", "题目")
    assert result.content == QUIZ_JSON


def test_two_object_wrapper_content_is_not_silently_first_picked():
    content = two_object_wrapper_content()
    transport = RecordingTransport(message={"content": content})
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    result = llm.complete("quiz", "只返回 JSON。", "题目")
    # Not the embedded example — and not a silently wrong single object
    # either: the content passes through untouched so the client's strict
    # JSON gate rejects it honestly.
    example = json.dumps({"title": "示例格式", "questions": []}, ensure_ascii=False)
    assert result.content == content
    assert result.content != example
    assert QUIZ_JSON not in result.content or "```" in result.content
    with pytest.raises(ValueError):
        json.loads(result.content)
    # The pass-through is honest metering, not a hidden failure: the vendor
    # really completed, so the charge is real and unchanged.
    assert result.millipoints == WINDOW_C_MILLIPOINTS


# --- relay-level: normalized content, honest settlement, and the mode ---


def test_route_quiz_serves_normalized_json_with_honest_settlement_and_response_mode(
    tmp_path,
):
    transport = RecordingTransport(message={"content": thinking_shaped_quiz_content()})
    llm = BailianLlm("key", "https://bailian.example/v1", post=transport)
    client, token = make_relay_client(tmp_path, llm.complete)
    before = llm_balance(tmp_path)
    response = llm_request(client, token)
    assert response.status_code == 200
    payload = response.json()
    # The client-facing content is exactly one clean JSON object.
    assert payload["content"] == QUIZ_JSON
    assert payload["points_charged"] == 0.011  # 11 millipoints / 1000
    assert llm_balance(tmp_path) == before - WINDOW_C_MILLIPOINTS
    # The usage row records the REAL vendor usage, not the normalized text.
    rows = llm_usage_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["operation"] == "quiz"
    assert rows[0]["model"] == "qwen3.5-flash"
    assert rows[0]["input_tokens"] == WINDOW_C_USAGE["prompt_tokens"]
    assert rows[0]["output_tokens"] == WINDOW_C_USAGE["completion_tokens"]
    assert rows[0]["millipoints"] == WINDOW_C_MILLIPOINTS
    # The vendor request carried the JSON response mode the fix adds.
    body = transport.calls[0]["json"]
    assert body["response_format"] == {"type": "json_object"}
    assert body["enable_thinking"] is False
