"""Request size caps (VAL-SECU-016): 413 for oversized payloads.

Pinned defaults (library/relay-dedup-design.md, "Security hardening"):
/v1/llm JSON body <= 250 KB; /v1/transcribe audio <= 500 MB / 4 h. The
byte caps are enforced (a) early on the declared Content-Length before
the body is parsed/buffered and (b) authoritatively while spooling the
upload (5xx-free abort). The 4-hour duration cap can only be known from
the vendor's own ``duration_seconds`` (the relay has no decoding
dependency), so it is enforced on the miss path BEFORE any charge or
store: an over-cap clip is refused 413, never charged, never stored,
and identical retries stay misses.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from server import accounts, relay, transcript_store
from server.llm import Completion


def make_client(tmp_path, streaming_asr_fn=None, llm_fn=None, quota_seconds=10**9):
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
    )
    return TestClient(app), token


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def upload(client, token, audio=b"fake-audio"):
    return client.post(
        "/v1/transcribe",
        headers=auth(token),
        files={"audio": ("a.m4a", audio, "audio/mp4")},
    )


class CountingAsr:
    def __init__(self, duration_seconds=1):
        self.calls = 0
        self.duration_seconds = duration_seconds

    def __call__(self, source, filename):
        self.calls += 1
        source.read()
        return {
            "language": "zh",
            "duration_seconds": self.duration_seconds,
            "segments": [{"start": 0.0, "text": "第一讲"}],
        }


class CountingLlm:
    def __init__(self):
        self.calls = 0

    def __call__(self, operation, system, user):
        self.calls += 1
        return Completion("内容", 100, 20, 7, "qwen3.5-flash")


# --- pinned defaults ---


def test_pinned_llm_body_cap_is_250_kb():
    assert relay.LLM_BODY_MAX_BYTES == 250 * 1024


def test_pinned_transcribe_byte_cap_is_500_mb():
    assert relay.TRANSCRIBE_MAX_BYTES == 500 * 1024 * 1024


def test_pinned_transcribe_duration_cap_is_four_hours():
    assert relay.TRANSCRIBE_MAX_SECONDS == 4 * 3600


# --- /v1/llm body cap ---


def test_llm_body_over_250_kb_returns_413_before_vendor_and_charge(tmp_path):
    vendor = CountingLlm()
    client, token = make_client(tmp_path, llm_fn=vendor)
    oversized = "x" * (relay.LLM_BODY_MAX_BYTES + 1024)

    response = client.post(
        "/v1/llm",
        headers=auth(token),
        json={"operation": "qa", "system": "系统", "user": oversized},
    )
    assert response.status_code == 413
    assert vendor.calls == 0  # the vendor was never reached
    quota = client.get("/v1/quota", headers=auth(token)).json()
    assert quota["llm_points_remaining"] == 100.0  # nothing was charged


def test_llm_body_under_250_kb_succeeds(tmp_path):
    vendor = CountingLlm()
    client, token = make_client(tmp_path, llm_fn=vendor)
    under = "y" * (relay.LLM_BODY_MAX_BYTES - 8192)  # headroom for JSON framing

    response = client.post(
        "/v1/llm",
        headers=auth(token),
        json={"operation": "qa", "system": "系统", "user": under},
    )
    assert response.status_code == 200
    assert vendor.calls == 1


def test_llm_chunked_oversize_body_without_content_length_returns_413(tmp_path):
    """No Content-Length (streamed body): the parsed-body check still refuses."""
    vendor = CountingLlm()
    client, token = make_client(tmp_path, llm_fn=vendor)

    def stream_body():
        yield '{"operation": "qa", "system": "系统", "user": "'.encode()
        yield b"x" * (relay.LLM_BODY_MAX_BYTES + 1024)
        yield b'"}'

    headers = {"content-type": "application/json", **auth(token)}
    response = client.post("/v1/llm", headers=headers, content=stream_body())
    assert response.status_code == 413
    assert vendor.calls == 0


# --- /v1/transcribe byte cap ---


def test_spool_aborts_past_the_byte_cap(monkeypatch):
    with pytest.raises(transcript_store.TranscriptTooLarge):
        transcript_store.spool_and_hash(BytesIO(b"x" * 4096), max_bytes=1024)
    # Without a cap the full stream still spools exactly as before.
    spooled, _ = transcript_store.spool_and_hash(BytesIO(b"x" * 4096))
    assert spooled.read() == b"x" * 4096
    spooled.close()


def test_transcribe_over_byte_cap_returns_413_before_vendor(tmp_path, monkeypatch):
    monkeypatch.setenv(relay.TRANSCRIBE_MAX_BYTES_ENV, "1024")
    vendor = CountingAsr()
    client, token = make_client(tmp_path, streaming_asr_fn=vendor)

    response = upload(client, token, audio=b"x" * 8192)
    assert response.status_code == 413
    assert vendor.calls == 0  # never spooled to the vendor

    quota = client.get("/v1/quota", headers=auth(token)).json()
    assert quota["transcribe_seconds_remaining"] == 10**9  # untouched


def test_bad_byte_cap_env_falls_back_to_the_pinned_default(monkeypatch):
    monkeypatch.setenv(relay.TRANSCRIBE_MAX_BYTES_ENV, "not-a-number")
    assert relay.transcribe_max_bytes() == relay.TRANSCRIBE_MAX_BYTES


# --- /v1/transcribe 4-hour duration cap ---


def test_transcribe_over_four_hour_duration_cap_is_413_uncharged_unstored(tmp_path):
    vendor = CountingAsr(duration_seconds=relay.TRANSCRIBE_MAX_SECONDS + 1)
    client, token = make_client(tmp_path, streaming_asr_fn=vendor)

    response = upload(client, token, audio=b"four-plus-hours")
    assert response.status_code == 413
    assert vendor.calls == 1  # duration is only known from the upstream result

    quota = client.get("/v1/quota", headers=auth(token)).json()
    assert quota["transcribe_seconds_remaining"] == 10**9  # never charged
    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transcript_reuse").fetchone()[0] == 0
    finally:
        conn.close()


def test_identical_over_cap_clip_is_a_miss_again_not_a_stored_hit(tmp_path):
    vendor = CountingAsr(duration_seconds=relay.TRANSCRIBE_MAX_SECONDS + 1)
    client, token = make_client(tmp_path, streaming_asr_fn=vendor)

    assert upload(client, token, audio=b"no-phantom-row").status_code == 413
    assert upload(client, token, audio=b"no-phantom-row").status_code == 413
    assert vendor.calls == 2  # nothing was stored after the first refusal


def test_transcribe_at_exactly_four_hours_succeeds_and_charges(tmp_path):
    vendor = CountingAsr(duration_seconds=relay.TRANSCRIBE_MAX_SECONDS)
    client, token = make_client(tmp_path, streaming_asr_fn=vendor)

    response = upload(client, token, audio=b"exactly-four-hours")
    assert response.status_code == 200
    quota = client.get("/v1/quota", headers=auth(token)).json()
    assert quota["transcribe_seconds_remaining"] == 10**9 - relay.TRANSCRIBE_MAX_SECONDS
