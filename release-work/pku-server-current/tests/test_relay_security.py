"""Relay session auth, quota gates, and activation/redemption single-use.

Pins the security contract route by route (contract-work/04
route-by-route table, VAL-SECU-001..009 sequential legs): every
protected route answers 401 for every malformed auth state; the 402
pre-checks refuse before any vendor call; each unconfigured provider
answers 503 with its route-specific detail; upstream failures answer
502 without echoing upstream bodies, credentials, or key material;
/v1/recharge stays a 501 stub pointing at /v1/redeem; activation is
single-use and creates the session; redemption credits exactly the
code's value once. Genuinely overlapping requests live in
test_relay_security_concurrency.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from server import accounts, relay
from server.asr import AsrError
from server.llm import Completion, LlmError

# Seeded upstream material that must never reach a 502 response.
SEED_UPSTREAM_BODY = "upstream-secret-body-9f8e7d6c5b4a"
SEED_API_KEY = "sk-SEEDED-KEY-0123456789abcdef"
SEED_OAUTH_CODE = "oauth-seed-code-7f3e9a"


def make_security_client(
    tmp_path,
    asr_fn=None,
    streaming_asr_fn=None,
    llm_fn=None,
    notion_exchange_fn=None,
    quota_seconds=600,
    llm_millipoints=accounts.MONTHLY_LLM_MILLIPOINTS,
):
    """One app on a fresh DB with one provisioned account (同学A)."""
    conn = accounts.connect(tmp_path / "accounts.db")
    account_id = accounts.create_account(conn, "同学A")
    conn.execute(
        "UPDATE accounts SET transcribe_seconds_remaining = ?,"
        " llm_millipoints_remaining = ? WHERE id = ?",
        (quota_seconds, llm_millipoints, account_id),
    )
    conn.commit()
    token = accounts.issue_session(conn, account_id)
    conn.close()
    app = relay.create_relay_app(
        tmp_path / "accounts.db",
        asr_fn=asr_fn,
        streaming_asr_fn=streaming_asr_fn,
        llm_fn=llm_fn,
        notion_exchange_fn=notion_exchange_fn,
    )
    return TestClient(app), token


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def upload(client, token=None, audio=b"fake-audio"):
    headers = auth(token) if token else {}
    return client.post(
        "/v1/transcribe",
        headers=headers,
        files={"audio": ("a.m4a", audio, "audio/mp4")},
    )


def llm_request(client, token=None):
    return client.post(
        "/v1/llm",
        headers=auth(token) if token else {},
        json={"operation": "qa", "system": "系统", "user": "问题"},
    )


def exchange_request(client, token=None, code="not-a-real-code"):
    return client.post(
        "/v1/notion/exchange",
        headers=auth(token) if token else {},
        json={"code": code, "redirect_uri": "http://localhost:8765/callback"},
    )


# --- VAL-SECU-001: the 401 matrix, every route x every malformed state ---


PROTECTED_ROUTES = (
    "/v1/transcribe",
    "/v1/quota",
    "/v1/llm",
    "/v1/redeem",
    "/v1/notion/exchange",
)


def malformed_auth_states(tmp_path):
    """The six malformed auth states, as header dicts (or None for missing)."""
    conn = accounts.connect(tmp_path / "accounts.db")
    expired_id = accounts.create_account(conn, "过期同学")
    expired_token = accounts.issue_session(conn, expired_id, ttl=-1)
    disabled_id = accounts.create_account(conn, "停用同学")
    disabled_token = accounts.issue_session(conn, disabled_id)
    conn.execute("UPDATE accounts SET status = 'disabled' WHERE id = ?", (disabled_id,))
    conn.commit()
    conn.close()
    return {
        "missing Authorization header": None,
        "non-Bearer scheme": {"Authorization": "Basic abcdef"},
        "empty token": {"Authorization": "Bearer "},
        "unknown token": {"Authorization": "Bearer not-a-real-session-token"},
        "expired session": {"Authorization": f"Bearer {expired_token}"},
        "disabled account": {"Authorization": f"Bearer {disabled_token}"},
    }


def request_route(client, path, headers):
    """A minimal valid request for each protected route under any auth state."""
    if path == "/v1/transcribe":
        return client.post(
            path,
            headers=headers,
            files={"audio": ("a.m4a", b"fake-audio", "audio/mp4")},
        )
    if path == "/v1/quota":
        return client.get(path, headers=headers)
    if path == "/v1/llm":
        return client.post(
            path, headers=headers, json={"operation": "qa", "system": "系统", "user": "问题"}
        )
    if path == "/v1/redeem":
        return client.post(path, headers=headers, json={"code": "ABCD1234"})
    return client.post(
        path,
        headers=headers,
        json={"code": "not-a-real-code", "redirect_uri": "http://localhost:8765/callback"},
    )


def test_every_protected_route_401_for_every_malformed_auth_state(tmp_path):
    client, _ = make_security_client(tmp_path)
    states = malformed_auth_states(tmp_path)
    for path in PROTECTED_ROUTES:
        for state, headers in states.items():
            response = request_route(client, path, headers)
            assert response.status_code == 401, f"{path} x {state}"


# --- VAL-SECU-002/003 (pre-check legs): 402 refuses before any vendor call ---


class CountingAsr:
    """Streaming fake with a call counter — proves the 402 gate precedes it."""

    def __init__(self):
        self.calls = 0

    def __call__(self, source, filename):
        self.calls += 1
        return {
            "language": "zh",
            "duration_seconds": 120,
            "segments": [{"start": 0.0, "text": "第一讲"}],
        }


class CountingLlm:
    def __init__(self):
        self.calls = 0

    def __call__(self, operation, system, user):
        self.calls += 1
        return Completion("内容", 100, 20, 7, "qwen3.5-flash")


def test_transcribe_402_pre_check_never_invokes_the_vendor(tmp_path):
    vendor = CountingAsr()
    client, token = make_security_client(tmp_path, streaming_asr_fn=vendor, quota_seconds=0)
    response = upload(client, token)
    assert response.status_code == 402
    assert vendor.calls == 0


def test_llm_402_pre_check_never_invokes_the_vendor(tmp_path):
    vendor = CountingLlm()
    client, token = make_security_client(tmp_path, llm_fn=vendor, llm_millipoints=0, quota_seconds=0)
    response = llm_request(client, token)
    assert response.status_code == 402
    assert vendor.calls == 0


# --- VAL-SECU-004: 503 per unconfigured provider, route-specific detail ---


def test_transcribe_503_when_asr_vendor_unconfigured(tmp_path):
    client, token = make_security_client(tmp_path)  # no asr_fn / streaming_asr_fn
    response = upload(client, token)
    assert response.status_code == 503
    assert response.json()["detail"] == "ASR vendor not configured yet"


def test_llm_503_when_llm_vendor_unconfigured(tmp_path):
    client, token = make_security_client(tmp_path)
    response = llm_request(client, token)
    assert response.status_code == 503
    assert response.json()["detail"] == "LLM vendor not configured yet"


def test_exchange_503_when_oauth_unconfigured(tmp_path):
    client, token = make_security_client(tmp_path)
    response = exchange_request(client, token)
    assert response.status_code == 503
    assert response.json()["detail"] == "Notion OAuth is not configured"


# --- VAL-SECU-005: 502 details carry no upstream body/credential/key material ---


def test_asr_502_detail_carries_no_upstream_body_or_credential(tmp_path):
    def failing_vendor(source, filename):
        raise AsrError(
            f"DashScope submission failed: body={SEED_UPSTREAM_BODY}"
            f" api_key={SEED_API_KEY}"
        )

    client, token = make_security_client(tmp_path, streaming_asr_fn=failing_vendor)
    response = upload(client, token)
    assert response.status_code == 502
    assert SEED_UPSTREAM_BODY not in response.text
    assert SEED_API_KEY not in response.text
    quota = client.get("/v1/quota", headers=auth(token)).json()
    assert quota["transcribe_seconds_remaining"] == 600  # the 502 is uncharged


def test_llm_502_detail_carries_no_upstream_body_or_credential(tmp_path):
    def failing_llm(operation, system, user):
        raise LlmError(
            f"Bailian LLM failed (HTTP 500) body={SEED_UPSTREAM_BODY}"
            f" Bearer {SEED_API_KEY}"
        )

    client, token = make_security_client(tmp_path, llm_fn=failing_llm)
    response = llm_request(client, token)
    assert response.status_code == 502
    assert SEED_UPSTREAM_BODY not in response.text
    assert SEED_API_KEY not in response.text
    quota = client.get("/v1/quota", headers=auth(token)).json()
    assert quota["llm_points_remaining"] == 100.0  # the 502 is uncharged


def test_exchange_502_is_generic_without_code_or_provider_body(tmp_path):
    def failing_exchange(code, redirect_uri):
        raise RuntimeError(
            f"provider rejected code={code} body={SEED_UPSTREAM_BODY}"
            f" secret={SEED_API_KEY}"
        )

    client, token = make_security_client(tmp_path, notion_exchange_fn=failing_exchange)
    response = exchange_request(client, token, code=SEED_OAUTH_CODE)
    assert response.status_code == 502
    for seed in (SEED_OAUTH_CODE, SEED_UPSTREAM_BODY, SEED_API_KEY):
        assert seed not in response.text


# --- VAL-SECU-006: the reserved payment endpoint is a 501 stub ---


def test_recharge_is_always_501_pointing_at_redeem(tmp_path):
    client, token = make_security_client(tmp_path)

    bare = client.post("/v1/recharge")
    assert bare.status_code == 501
    assert "/v1/redeem" in bare.json()["detail"]

    authenticated = client.post("/v1/recharge", headers=auth(token), json={"amount": 100})
    assert authenticated.status_code == 501
    assert "/v1/redeem" in authenticated.json()["detail"]


# --- VAL-SECU-007: activation is single-use and creates the session ---


def test_activation_is_single_use_and_creates_the_session(tmp_path):
    client, _ = make_security_client(tmp_path)
    conn = accounts.connect(tmp_path / "accounts.db")
    code = accounts.create_code(conn, 720)
    conn.close()

    first = client.post("/v1/activate", json={"code": code})
    assert first.status_code == 200
    body = first.json()
    assert body["transcribe_seconds_remaining"] == 720
    assert body["platform_token"]

    # The session exists and authenticates with the code's transcription quota.
    quota = client.get("/v1/quota", headers=auth(body["platform_token"])).json()
    assert quota["transcribe_seconds_remaining"] == 720

    # The DB marks the code redeemed by exactly the account the session opens.
    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        code_row = conn.execute(
            "SELECT redeemed_by, redeemed_at FROM codes WHERE code = ?", (code,)
        ).fetchone()
        account = accounts.validate_session(conn, body["platform_token"])
        assert account is not None
        assert code_row["redeemed_by"] == account["id"]
        assert code_row["redeemed_at"] is not None
        # the activation created exactly this one session for that account
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE account_id = ?", (account["id"],)
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()

    # Second use of the same code is rejected.
    assert client.post("/v1/activate", json={"code": code}).status_code == 400


# --- VAL-SECU-009: redemption credits exactly the code's value once ---


def test_redemption_credits_exactly_the_code_value_once(tmp_path):
    client, token = make_security_client(tmp_path, quota_seconds=600)
    conn = accounts.connect(tmp_path / "accounts.db")
    code = accounts.create_code(conn, 3600)
    conn.close()

    response = client.post("/v1/redeem", headers=auth(token), json={"code": code})
    assert response.status_code == 200
    assert response.json() == {"transcribe_seconds_remaining": 4200}  # 600 + 3600

    # The code is redeemed by exactly this account.
    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        code_row = conn.execute(
            "SELECT redeemed_by FROM codes WHERE code = ?", (code,)
        ).fetchone()
        account = accounts.validate_session(conn, token)
        assert code_row["redeemed_by"] == account["id"]
    finally:
        conn.close()

    # Second use and unknown codes are rejected; the balance never moves twice.
    assert (
        client.post("/v1/redeem", headers=auth(token), json={"code": code}).status_code
        == 400
    )
    assert (
        client.post("/v1/redeem", headers=auth(token), json={"code": "NOPE1234"}).status_code
        == 400
    )
    quota = client.get("/v1/quota", headers=auth(token)).json()
    assert quota["transcribe_seconds_remaining"] == 4200
