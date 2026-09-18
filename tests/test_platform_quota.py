"""Client-bridge quota (VAL-SECU-025): llm points + transcription seconds.

`platform.quota()` surfaces the relay's `llm_points_remaining` alongside the
transcription seconds from `/v1/quota`, never reproduces any token value in
the returned payload (seeded-token scan over the JSON and the panel API
surface), and reports honest states: an unreachable relay is `available:
false` without fabricating a balance, and an inactive account is the explicit
not-activated shape (`active: false`). The student-panel rendering of the
balance is VAL-EXER-030 (M3).
"""

from __future__ import annotations

import json as json_lib
from types import SimpleNamespace

import httpx
import pytest

from pku_sync import platform

RELAY_QUOTA_BODY = {
    "label": "同学A",
    "transcribe_seconds_remaining": 3600,
    "llm_points_remaining": 100.0,
}


def make_settings(**overrides) -> SimpleNamespace:
    base = dict(
        cloud_transcribe_url="https://relay.example/v1/transcribe",
        platform_token="",
        transcription_backend="local",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def fake_get(payload, status=200):
    """An httpx.get stand-in that records auth and returns the given body."""

    def handler(url, headers, timeout):
        return httpx.Response(status, json=payload)

    return handler


# -- llm_points_remaining alongside transcription seconds ----------------------


def test_quota_returns_llm_points_remaining_alongside_seconds(monkeypatch):
    fake_relay = fake_get(dict(RELAY_QUOTA_BODY))
    monkeypatch.setattr(platform.httpx, "get", fake_relay)
    settings = make_settings(platform_token="token-7")
    result = platform.quota(settings)
    assert result == {
        "active": True,
        "available": True,
        "transcribe_seconds_remaining": 3600,
        "llm_points_remaining": 100.0,
    }


def test_quota_omits_llm_points_when_relay_does_not_provide(monkeypatch):
    # keeps the pre-existing exact-shape contract: the key appears only when
    # the relay actually returns it (no fabricated points value)
    monkeypatch.setattr(
        platform.httpx,
        "get",
        fake_get({"transcribe_seconds_remaining": 3600}),
    )
    result = platform.quota(make_settings(platform_token="token-7"))
    assert "llm_points_remaining" not in result
    assert result["transcribe_seconds_remaining"] == 3600


# -- no token values anywhere in quota responses (seeded-token scan) -----------


def test_quota_returned_payload_never_contains_the_platform_token(monkeypatch):
    seeded = "seeded-platform-token-9f8e7"
    seen_auth = {}

    def echo_all(url, headers, timeout):
        # hostile relay: reflects every received key back into the JSON
        seen_auth["Authorization"] = headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "transcribe_seconds_remaining": 600,
                "llm_points_remaining": 42.0,
                "platform_token": seeded,
                "access_token": seeded,
                "k": "v",
            },
        )

    monkeypatch.setattr(platform.httpx, "get", echo_all)
    result = platform.quota(make_settings(platform_token=seeded))
    assert seen_auth["Authorization"] == f"Bearer {seeded}"
    # the token is used only as the request credential, never in the response
    rendered = json_lib.dumps(result, ensure_ascii=False)
    assert seeded not in rendered
    assert set(result) == {"active", "available", "transcribe_seconds_remaining", "llm_points_remaining"}


def test_quota_panel_api_payload_contains_no_token(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from pku_sync.panel import webapi

    seeded = "seeded-panel-token-abc123"
    monkeypatch.setattr(
        platform.httpx,
        "get",
        fake_get(
            {
                "transcribe_seconds_remaining": 720,
                "llm_points_remaining": 5.5,
                "label": "同学A",
            }
        ),
    )
    settings = SimpleNamespace(
        data_dir=tmp_path,
        cloud_transcribe_url="https://relay.example/v1/transcribe",
        platform_token=seeded,
        transcription_backend="cloud",
    )
    client = TestClient(webapi.create_app(settings))
    response = client.get("/api/platform/quota")
    assert response.status_code == 200
    payload = response.json()
    assert payload["transcribe_seconds_remaining"] == 720
    assert payload["llm_points_remaining"] == 5.5
    # seeded-token scan over the exact API surface
    assert seeded not in json_lib.dumps(payload, ensure_ascii=False)
    assert seeded not in response.text


# -- honest states: unreachable relay and inactive account ----------------------


def test_quota_unreachable_relay_is_honest_unavailable_state(monkeypatch):
    def unreachable(url, headers, timeout):
        raise httpx.ConnectError("relay down")

    monkeypatch.setattr(platform.httpx, "get", unreachable)
    result = platform.quota(make_settings(platform_token="token-7"))
    assert result == {"active": True, "available": False}
    # no fabricated balance in the degraded state
    assert "transcribe_seconds_remaining" not in result
    assert "llm_points_remaining" not in result


def test_quota_relay_error_status_is_honest_unavailable(monkeypatch):
    monkeypatch.setattr(
        platform.httpx, "get", fake_get({"detail": "boom"}, status=500)
    )
    result = platform.quota(make_settings(platform_token="token-7"))
    assert result == {"active": True, "available": False}


def test_quota_inactive_account_is_explicit_not_activated_shape(monkeypatch):
    called = {"n": 0}

    def must_not_be_called(*args, **kwargs):
        called["n"] += 1
        return httpx.Response(500, json={})

    monkeypatch.setattr(platform.httpx, "get", must_not_be_called)
    result = platform.quota(make_settings(platform_token=""))
    assert result == {"active": False}  # explicit not-activated shape
    assert called["n"] == 0  # no network call for an inactive account


def test_quota_requires_a_token_to_even_ask(monkeypatch):
    monkeypatch.setattr(
        platform.httpx,
        "get",
        fake_get(dict(RELAY_QUOTA_BODY)),
    )
    result = platform.quota(make_settings(platform_token=" "))
    assert result == {"active": False}
