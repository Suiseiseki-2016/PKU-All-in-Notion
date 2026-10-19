"""Exact /v1/llm metering: relay-level allowlist, charge fidelity,
model-price selection, usage rows, and the 402 boundary.

Pins the LLM metering contract (VAL-SECU-011..014, VAL-EXER-034): the
relay ITSELF rejects any operation outside {notes, summary, quiz,
grade, qa} with 400 BEFORE the vendor is invoked — the check inside
BailianLlm.complete is defense-in-depth, not the enforcement point
(contract-work/04 edge: a custom llm_fn can ignore ALLOWED_OPERATIONS,
so the fake vendor here accepts everything and the counter must stay
at zero for disallowed operations). A charge equals exactly the
provider completion: the fake llm_fn returns known tokens/millipoints,
the balance decreases by exactly that, and the settlement is
{content, points_charged} with points_charged = millipoints/1000.
Model-price selection uses the requested model's price row
(hand-computed case) with an explicit fallback to flash prices for
unknown models that still completes. Every successful call writes one
llm_usage row carrying operation, model, input/output tokens, and
millipoints. The grade and quiz legs meter exactly and atomically;
the boundary remaining < charge answers 402 with no partial
decrement and no usage row.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from server import accounts, relay
from server.llm import ALLOWED_OPERATIONS, BailianLlm, Completion, LlmError

# The approved operation set, pinned at the relay surface.
ALLOWED = ("notes", "summary", "quiz", "grade", "qa")
DISALLOWED = ("chat", "translate", "summarize", "grading", "NOTES", "quiz ", "")


def make_metering_client(
    tmp_path, llm_fn=None, llm_millipoints=accounts.MONTHLY_LLM_MILLIPOINTS
):
    """One app on a fresh DB with one provisioned account (同学A)."""
    conn = accounts.connect(tmp_path / "accounts.db")
    account_id = accounts.create_account(conn, "同学A")
    conn.execute(
        "UPDATE accounts SET llm_millipoints_remaining = ? WHERE id = ?",
        (llm_millipoints, account_id),
    )
    conn.commit()
    token = accounts.issue_session(conn, account_id)
    conn.close()
    app = relay.create_relay_app(tmp_path / "accounts.db", llm_fn=llm_fn)
    return TestClient(app), token


def llm_call(client, token, operation="qa", system="系统", user="问题"):
    return client.post(
        "/v1/llm",
        headers={"Authorization": f"Bearer {token}"},
        json={"operation": operation, "system": system, "user": user},
    )


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
            "SELECT account_id, operation, model, input_tokens, output_tokens,"
            " millipoints FROM llm_usage ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


class AcceptEverythingLlm:
    """Fake llm_fn that accepts ANY operation, with a call counter.

    The relay-level allowlist must hold even when the vendor enforces
    nothing — ``calls`` proves what the relay gate let through.
    """

    def __init__(self, completion=None):
        self.calls = 0
        self.operations = []
        self.completion = completion or Completion("内容", 100, 20, 7, "qwen3.5-flash")

    def __call__(self, operation, system, user):
        self.calls += 1
        self.operations.append(operation)
        return self.completion


# --- VAL-SECU-011: the relay enforces the operation allowlist itself ---


def test_disallowed_operation_is_400_before_the_vendor(tmp_path):
    vendor = AcceptEverythingLlm()
    client, token = make_metering_client(tmp_path, llm_fn=vendor)
    for operation in DISALLOWED:
        response = llm_call(client, token, operation=operation)
        assert response.status_code == 400, operation
        assert vendor.calls == 0, operation  # the relay never reached the vendor
    # Rejected operations charge nothing and record nothing.
    assert llm_balance(tmp_path) == accounts.MONTHLY_LLM_MILLIPOINTS
    assert llm_usage_rows(tmp_path) == []


def test_every_allowed_operation_reaches_the_vendor(tmp_path):
    vendor = AcceptEverythingLlm()
    client, token = make_metering_client(tmp_path, llm_fn=vendor)
    for operation in ALLOWED:
        response = llm_call(client, token, operation=operation)
        assert response.status_code == 200, operation
        assert response.json() == {"content": "内容", "points_charged": 0.007}
    assert vendor.calls == 5
    assert sorted(vendor.operations) == sorted(ALLOWED)
    # The relay surface and the vendor share one pinned operation set.
    assert set(ALLOWED) == ALLOWED_OPERATIONS


# --- VAL-SECU-012: the charge equals the actual provider usage ---


def test_charge_equals_vendor_millipoints_and_settlement_is_exact(tmp_path):
    completion = Completion("解析内容", 1234, 567, 42, "qwen3.5-flash")

    def fake_llm(operation, system, user):
        assert operation == "qa"
        return completion

    client, token = make_metering_client(tmp_path, llm_fn=fake_llm)
    before = llm_balance(tmp_path)

    response = llm_call(client, token, operation="qa")
    assert response.status_code == 200
    assert response.json() == {"content": "解析内容", "points_charged": 42 / 1000}

    # The balance decreases by exactly the completion's millipoints.
    assert llm_balance(tmp_path) == before - 42
    quota = client.get(
        "/v1/quota", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert quota["llm_points_remaining"] == (before - 42) / 1000


# --- VAL-SECU-013: model-price selection, hand-computed, with fallback ---


def bailian_post(url, headers, json, timeout):
    """Fake Bailian HTTP: fixed token usage for a hand-computed charge."""
    return httpx.Response(
        200,
        json={
            "model": json["model"],
            "choices": [{"message": {"content": "答案"}}],
            "usage": {"prompt_tokens": 100_000, "completion_tokens": 10_000},
        },
    )


def test_requested_model_price_row_is_used_hand_computed():
    llm = BailianLlm(
        "key", "https://bailian.example/v1", model="qwen3.7-plus", post=bailian_post
    )
    result = llm.complete("qa", "系统", "问题")
    # qwen3.7-plus row (¥2.0/M in, ¥8.0/M out):
    # (100_000 × 2.0 + 10_000 × 8.0) / 1_000_000 = ¥0.28 → × 10_000 = 2_800
    assert result.millipoints == 2_800
    assert result.model == "qwen3.7-plus"
    assert result.content == "答案"


def test_unknown_model_is_rejected_before_spending():
    llm = BailianLlm("key", "https://bailian.example/v1", model="qwen9-omni",
                     post=lambda *args, **kwargs: pytest.fail("unknown model sent upstream"))
    with pytest.raises(LlmError, match="unsupported AI model"):
        llm.complete("qa", "系统", "问题")


def test_relay_charges_the_hand_computed_model_price_end_to_end(tmp_path):
    """The whole chain: route → vendor → price row → charge, one settlement."""
    llm = BailianLlm(
        "key", "https://bailian.example/v1", model="qwen3.7-plus", post=bailian_post
    )
    client, token = make_metering_client(tmp_path, llm_fn=llm.complete)
    before = llm_balance(tmp_path)

    response = llm_call(client, token, operation="qa")
    assert response.status_code == 200
    assert response.json() == {"content": "答案", "points_charged": 2.8}
    assert llm_balance(tmp_path) == before - 2_800


# --- VAL-SECU-014: llm_usage records the actual completion ---


def test_llm_usage_row_records_operation_model_tokens_millipoints(tmp_path):
    def fake_llm(operation, system, user):
        return Completion("题目", 1000, 500, 42, "qwen3.7-plus")

    client, token = make_metering_client(tmp_path, llm_fn=fake_llm)
    assert llm_call(client, token, operation="quiz").status_code == 200

    rows = llm_usage_rows(tmp_path)
    assert len(rows) == 1
    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        account_id = conn.execute(
            "SELECT id FROM accounts WHERE label = '同学A'"
        ).fetchone()[0]
    finally:
        conn.close()
    row = rows[0]
    assert row["account_id"] == account_id
    assert row["operation"] == "quiz"
    assert row["model"] == "qwen3.7-plus"
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 500
    assert row["millipoints"] == 42


# --- VAL-EXER-034: grade and quiz meter exactly; the 402 boundary ---


def test_grade_meters_exactly_once_from_provider_tokens(tmp_path):
    vendor = AcceptEverythingLlm(Completion("批改结果", 2000, 800, 96, "qwen3.5-flash"))
    client, token = make_metering_client(tmp_path, llm_fn=vendor)
    before = llm_balance(tmp_path)

    response = llm_call(client, token, operation="grade")
    assert response.status_code == 200
    assert response.json() == {"content": "批改结果", "points_charged": 96 / 1000}
    assert vendor.operations == ["grade"]

    # Decremented exactly once, by exactly the provider-implied charge.
    assert llm_balance(tmp_path) == before - 96
    rows = llm_usage_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["operation"] == "grade"
    assert rows[0]["millipoints"] == 96

    # A second grade call charges once per call — never more.
    assert llm_call(client, token, operation="grade").status_code == 200
    assert llm_balance(tmp_path) == before - 2 * 96
    assert len(llm_usage_rows(tmp_path)) == 2


def test_quiz_meters_exactly_once_from_provider_tokens(tmp_path):
    vendor = AcceptEverythingLlm(Completion("题目", 100, 20, 7, "qwen3.5-flash"))
    client, token = make_metering_client(tmp_path, llm_fn=vendor)
    before = llm_balance(tmp_path)

    response = llm_call(client, token, operation="quiz")
    assert response.status_code == 200
    assert response.json() == {"content": "题目", "points_charged": 0.007}
    assert vendor.operations == ["quiz"]
    assert llm_balance(tmp_path) == before - 7
    rows = llm_usage_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["operation"] == "quiz"
    assert rows[0]["millipoints"] == 7


def test_remaining_below_charge_is_402_with_no_partial_decrement(tmp_path):
    """Balance 10 < charge 42: the atomic post-call charge refuses whole."""
    vendor = AcceptEverythingLlm(Completion("内容", 100, 20, 42, "qwen3.5-flash"))
    client, token = make_metering_client(
        tmp_path, llm_fn=vendor, llm_millipoints=10
    )

    response = llm_call(client, token, operation="grade")
    assert response.status_code == 402
    assert response.json()["detail"] == "AI points exhausted"
    # The boundary is refused by the atomic charge (the vendor completed,
    # the pre-check passed), so nothing partially decremented.
    assert vendor.calls == 1
    assert llm_balance(tmp_path) == 10
    assert llm_usage_rows(tmp_path) == []  # the refused charge records nothing
