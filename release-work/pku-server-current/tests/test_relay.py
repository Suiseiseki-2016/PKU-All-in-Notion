"""Cloud relay tests: auth, quota gate, metering (TestClient, fake ASR)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from server import accounts, relay


def fake_asr(audio: bytes) -> dict:
    return {
        "language": "zh",
        "duration_seconds": 120,
        "segments": [{"start": 0.0, "text": "第一讲"}],
    }


def make_client(tmp_path, asr_fn=None, quota_seconds=600):
    conn = accounts.connect(tmp_path / "accounts.db")
    account_id = accounts.create_account(conn, "同学A")
    if quota_seconds:
        conn.execute(
            "UPDATE accounts SET transcribe_seconds_remaining = ? WHERE id = ?",
            (quota_seconds, account_id),
        )
        conn.commit()
    token = accounts.issue_session(conn, account_id)
    conn.close()
    app = relay.create_relay_app(tmp_path / "accounts.db", asr_fn=asr_fn)
    return TestClient(app), token


def upload(client, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(
        "/v1/transcribe",
        headers=headers,
        files={"audio": ("a.m4a", b"fake-audio", "audio/mp4")},
    )


def test_transcribe_requires_session(tmp_path):
    client, _ = make_client(tmp_path)
    assert upload(client).status_code == 401


def test_transcribe_rejects_bad_token(tmp_path):
    client, _ = make_client(tmp_path)
    assert upload(client, "nonsense").status_code == 401


def test_transcribe_meters_and_returns_payload(tmp_path):
    client, token = make_client(tmp_path, asr_fn=fake_asr, quota_seconds=600)
    response = upload(client, token)
    assert response.status_code == 200
    body = response.json()
    assert body["segments"][0]["text"] == "第一讲"
    assert "duration_seconds" not in body  # metering field stays server-side
    quota = client.get("/v1/quota", headers={"Authorization": f"Bearer {token}"}).json()
    assert quota["label"] == "同学A"
    assert quota["transcribe_seconds_remaining"] == 480  # 600 - 120


def test_transcribe_refuses_when_quota_exhausted(tmp_path):
    client, token = make_client(tmp_path, asr_fn=fake_asr, quota_seconds=0)
    assert upload(client, token).status_code == 402


def test_transcribe_refuses_without_vendor(tmp_path):
    client, token = make_client(tmp_path, asr_fn=None)
    assert upload(client, token).status_code == 503


def test_healthz(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.get("/healthz").json() == {"ok": True}


def redeem(client, token, code):
    return client.post(
        "/v1/redeem",
        headers={"Authorization": f"Bearer {token}"},
        json={"code": code},
    )


def test_redeem_adds_quota(tmp_path):
    client, token = make_client(tmp_path, quota_seconds=0)
    conn = accounts.connect(tmp_path / "accounts.db")
    code = accounts.create_code(conn, 3600)
    conn.close()
    response = redeem(client, token, code)
    assert response.status_code == 200
    assert response.json() == {"transcribe_seconds_remaining": 3600}


def test_redeem_is_single_use(tmp_path):
    client, token = make_client(tmp_path, quota_seconds=0)
    conn = accounts.connect(tmp_path / "accounts.db")
    code = accounts.create_code(conn, 3600)
    conn.close()
    assert redeem(client, token, code).status_code == 200
    assert redeem(client, token, code).status_code == 400
    quota = client.get("/v1/quota", headers={"Authorization": f"Bearer {token}"}).json()
    assert quota["transcribe_seconds_remaining"] == 3600  # not double-credited


def test_redeem_rejects_unknown_code(tmp_path):
    client, token = make_client(tmp_path)
    assert redeem(client, token, "NOPE1234").status_code == 400


def test_redeem_requires_session(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.post("/v1/redeem", json={"code": "ABCD1234"}).status_code == 401


def test_recharge_is_a_501_stub(tmp_path):
    client, _ = make_client(tmp_path)
    response = client.post("/v1/recharge")
    assert response.status_code == 501
    assert "/v1/redeem" in response.json()["detail"]  # points at the live path
