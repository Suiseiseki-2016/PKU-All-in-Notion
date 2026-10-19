"""Per-route rate limits (VAL-SECU-015): 429 beyond the window, success under it.

Pinned limits (library/relay-dedup-design.md, "Security hardening"):
/v1/activate and /v1/redeem 10/min per IP; /v1/transcribe and /v1/llm
60/min per account; /v1/notion/exchange 10/min per account. Each test
builds a fresh app so its limiter window starts empty, then fires the
full allowance plus one and asserts the extra request answers 429
while every under-limit request succeeded. The sliding-window mechanics
are unit-tested in test_rate_limit.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from server import accounts, relay
from server.llm import Completion

RATE_LIMIT_N = {
    "/v1/activate": relay.RATE_LIMIT_ACTIVATE_PER_IP,
    "/v1/redeem": relay.RATE_LIMIT_REDEEM_PER_IP,
    "/v1/transcribe": relay.RATE_LIMIT_TRANSCRIBE_PER_ACCOUNT,
    "/v1/llm": relay.RATE_LIMIT_LLM_PER_ACCOUNT,
    "/v1/notion/exchange": relay.RATE_LIMIT_EXCHANGE_PER_ACCOUNT,
}


def make_client(
    tmp_path,
    streaming_asr_fn=None,
    llm_fn=None,
    notion_exchange_fn=None,
    quota_seconds=10**9,
):
    conn = accounts.connect(tmp_path / "accounts.db")
    account_id = accounts.create_account(conn, "同学A")
    conn.execute(
        "UPDATE accounts SET transcribe_seconds_remaining = ? WHERE id = ?",
        (quota_seconds, account_id),
    )
    conn.commit()
    token = accounts.issue_session(conn, account_id)
    conn.close()
    app = relay.create_relay_app(
        tmp_path / "accounts.db",
        streaming_asr_fn=streaming_asr_fn,
        llm_fn=llm_fn,
        notion_exchange_fn=notion_exchange_fn,
    )
    return TestClient(app), token


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def make_codes(tmp_path, count):
    conn = accounts.connect(tmp_path / "accounts.db")
    codes = [accounts.create_code(conn, 3600) for _ in range(count)]
    conn.close()
    return codes


class FakeStreamingAsr:
    def __init__(self, duration_seconds=1):
        self.calls = 0
        self.duration_seconds = duration_seconds

    def __call__(self, source, filename):
        self.calls += 1
        source.read()  # consume the spooled bytes the way the real vendor does
        return {
            "language": "zh",
            "duration_seconds": self.duration_seconds,
            "segments": [{"start": 0.0, "text": "第一讲"}],
        }


# --- /v1/activate: 10/min per IP ---


def test_activate_under_the_limit_then_429_beyond_ten_per_minute(tmp_path):
    client, _ = make_client(tmp_path)
    codes = make_codes(tmp_path, 11)

    for code in codes[:10]:
        response = client.post("/v1/activate", json={"code": code})
        assert response.status_code == 200, response.text  # under the limit succeed

    # The 11th attempt in the same window is refused even with a valid code.
    response = client.post("/v1/activate", json={"code": codes[10]})
    assert response.status_code == 429
    assert response.json()["detail"] == relay.RATE_LIMIT_MESSAGE

    # The 11th code was never consumed, so a fresh app on the same DB still
    # accepts it; the limiter is in-memory per app instance.
    fresh_app = relay.create_relay_app(tmp_path / "accounts.db")
    client2 = TestClient(fresh_app)
    assert client2.post("/v1/activate", json={"code": codes[10]}).status_code == 200


# --- /v1/redeem: 10/min per IP ---


def test_redeem_under_the_limit_then_429_beyond_ten_per_minute(tmp_path):
    client, token = make_client(tmp_path)
    codes = make_codes(tmp_path, 11)

    for code in codes[:10]:
        response = client.post("/v1/redeem", headers=auth(token), json={"code": code})
        assert response.status_code == 200, response.text

    response = client.post("/v1/redeem", headers=auth(token), json={"code": codes[10]})
    assert response.status_code == 429


# --- /v1/transcribe: 60/min per account ---


def test_transcribe_under_the_limit_then_429_beyond_sixty_per_minute(tmp_path):
    vendor = FakeStreamingAsr()
    client, token = make_client(tmp_path, streaming_asr_fn=vendor)

    for _ in range(60):
        response = client.post(
            "/v1/transcribe",
            headers=auth(token),
            files={"audio": ("a.m4a", b"same-audio", "audio/mp4")},
        )
        assert response.status_code == 200, response.text

    response = client.post(
        "/v1/transcribe",
        headers=auth(token),
        files={"audio": ("a.m4a", b"same-audio", "audio/mp4")},
    )
    assert response.status_code == 429
    assert vendor.calls == 1  # only the first (miss) ever reached the vendor


# --- /v1/llm: 60/min per account ---


def test_llm_under_the_limit_then_429_beyond_sixty_per_minute(tmp_path):
    def fake_llm(operation, system, user):
        return Completion("内容", 100, 20, 7, "qwen3.5-flash")

    client, token = make_client(tmp_path, llm_fn=fake_llm)

    for _ in range(60):
        response = client.post(
            "/v1/llm",
            headers=auth(token),
            json={"operation": "qa", "system": "系统", "user": "问题"},
        )
        assert response.status_code == 200, response.text

    response = client.post(
        "/v1/llm",
        headers=auth(token),
        json={"operation": "qa", "system": "系统", "user": "问题"},
    )
    assert response.status_code == 429


# --- /v1/notion/exchange: 10/min per account ---


def test_exchange_under_the_limit_then_429_beyond_ten_per_minute(tmp_path):
    calls = {"count": 0}

    def fake_exchange(code, redirect_uri):
        calls["count"] += 1
        return {"access_token": "JUST_A_DECOY_STRING_rate_test", "bot_id": "b"}

    client, token = make_client(tmp_path, notion_exchange_fn=fake_exchange)

    for _ in range(10):
        response = client.post(
            "/v1/notion/exchange",
            headers=auth(token),
            json={"code": "CODE1234", "redirect_uri": "http://localhost:8765/callback"},
        )
        assert response.status_code == 200, response.text
    assert calls["count"] == 10

    response = client.post(
        "/v1/notion/exchange",
        headers=auth(token),
        json={"code": "CODE1234", "redirect_uri": "http://localhost:8765/callback"},
    )
    assert response.status_code == 429
    assert calls["count"] == 10  # the refused request never reached Notion
