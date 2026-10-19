"""Notion OAuth relay hardening (VAL-SECU-017..019).

The relay exchanges the authorization code with server-held credentials
and hands the token payload back to the LOCAL client, but never persists
or logs the token, the code, or any provider body on either the success
or the failure path (DB tables, every file under the relay's data dir,
the captured log stream, and stdout/stderr are scanned for seeded
secrets). A failing exchange answers a generic 502 that echoes nothing.
redirect_uri is validated against the explicit allowlist
{8765, 8766}/callback before Notion is ever contacted.
"""

from __future__ import annotations

import base64
import logging

import httpx
from fastapi.testclient import TestClient

from server import accounts, notion_oauth, relay

# Deliberate decoy strings: every test asserts these appear NOWHERE
# (response, DB, files, logs). They are placeholders, never real secrets,
# and they keep the same form across the whole scan surface so a single
# substring proves the hygiene property.
HONEYPOT_STRING_ONE = "JUST_A_DECOY_STRING_9f8e7d6c5b4a"  # stands in for a token
HONEYPOT_STRING_TWO = "JUST_A_DECOY_STRING_c2f1d90e"  # stands in for a client secret
SEED_CODE = "oauth-seed-code-7f3e9a1b"
SEED_PROVIDER_BODY = "provider-raw-body-8842f3a0"

ALLOWED = ("http://localhost:8765/callback", "http://localhost:8766/callback")
DISALLOWED = (
    "http://localhost:9999/callback",
    "https://example.com/callback",
    "http://localhost:8765/callback/",
    "http://127.0.0.1:8765/callback",
    "",
)


def make_exchange_client(tmp_path, notion_exchange_fn):
    conn = accounts.connect(tmp_path / "accounts.db")
    account_id = accounts.create_account(conn, "同学A")
    token = accounts.issue_session(conn, account_id)
    conn.close()
    app = relay.create_relay_app(
        tmp_path / "accounts.db", notion_exchange_fn=notion_exchange_fn
    )
    return TestClient(app), token


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def exchange(
    client,
    token,
    code=SEED_CODE,
    redirect_uri="http://localhost:8765/callback",
):
    return client.post(
        "/v1/notion/exchange",
        headers=auth(token),
        json={"code": code, "redirect_uri": redirect_uri},
    )


def seed_scan(data: str, *seeds) -> list[str]:
    """Every seed that appears in ``data`` (an empty list is the clean state)."""
    return [seed for seed in seeds if seed in data]


# --- VAL-SECU-017: server-held secret use; token never persisted or logged ---


def test_successful_exchange_returns_token_payload_but_leaves_none_in_db_files_or_logs(
    tmp_path, caplog, capsys
):
    seen = []

    def fake_exchange(code, redirect_uri):
        seen.append((code, redirect_uri))
        return {
            "access_token": HONEYPOT_STRING_ONE,
            "bot_id": "bot-xyz",
            "workspace_name": "work",
        }

    client, token = make_exchange_client(tmp_path, fake_exchange)
    with caplog.at_level(logging.DEBUG):
        response = exchange(client, token)

    # The token payload is returned to the local client (by design), and the
    # mock received exactly the submitted code and redirect URI.
    assert response.status_code == 200
    assert response.json()["access_token"] == HONEYPOT_STRING_ONE
    assert seen == [(SEED_CODE, "http://localhost:8765/callback")]

    # No DB table carries the token or the code.
    conn = accounts.connect(tmp_path / "accounts.db")
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        assert tables, "expected the schema to exist"
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                dump = str(dict(row))
                assert HONEYPOT_STRING_ONE not in dump, f"{table} leaked the token"
                assert SEED_CODE not in dump, f"{table} leaked the code"
    finally:
        conn.close()

    # No file under the relay's own data dir contains the token or code.
    for path in tmp_path.rglob("*"):
        if not path.is_file():
            continue
        blob = path.read_bytes()
        assert HONEYPOT_STRING_ONE.encode() not in blob, f"{path} contains the token"
        assert SEED_CODE.encode() not in blob, f"{path} contains the code"

    # The captured log stream and stdout/stderr contain no secret material.
    out = capsys.readouterr()
    assert seed_scan(out.out, HONEYPOT_STRING_ONE, SEED_CODE, HONEYPOT_STRING_TWO) == []
    assert seed_scan(out.err, HONEYPOT_STRING_ONE, SEED_CODE, HONEYPOT_STRING_TWO) == []
    for record in caplog.records:
        assert (
            seed_scan(
                record.getMessage(), HONEYPOT_STRING_ONE, SEED_CODE, HONEYPOT_STRING_TWO
            )
            == []
        )


def test_notion_oauth_builds_basic_auth_from_server_held_secret_only(
    monkeypatch,
):
    """The real vendor adapter uses the relay env secret, never client input."""
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["authorization"] = headers["Authorization"]
        captured["json"] = json
        return httpx.Response(
            200,
            json={
                "access_token": HONEYPOT_STRING_ONE,
                "bot_id": "bot-xyz",
                "workspace_name": "work",
            },
        )

    monkeypatch.setattr(notion_oauth.httpx, "post", fake_post)
    client = notion_oauth.NotionOAuth(
        client_id="cid-123", client_secret=HONEYPOT_STRING_TWO
    )
    payload = client.exchange(SEED_CODE, "http://localhost:8765/callback")

    assert payload["access_token"] == HONEYPOT_STRING_ONE
    assert captured["url"] == "https://api.notion.com/v1/oauth/token"
    expected = base64.b64encode(
        f"cid-123:{HONEYPOT_STRING_TWO}".encode()
    ).decode()
    assert captured["authorization"] == f"Basic {expected}"
    assert captured["json"] == {
        "grant_type": "authorization_code",
        "code": SEED_CODE,
        "redirect_uri": "http://localhost:8765/callback",
    }


# --- VAL-SECU-018: a failing exchange is a generic 502 with no echo ---


def test_failing_exchange_is_generic_502_with_no_seed_in_response_or_logs(
    tmp_path, caplog, capsys
):
    def failing_exchange(code, redirect_uri):
        raise RuntimeError(
            f"Notion rejected code={code} body={SEED_PROVIDER_BODY}"
            f" decoy={HONEYPOT_STRING_TWO}"
        )

    client, token = make_exchange_client(tmp_path, failing_exchange)
    with caplog.at_level(logging.DEBUG):
        response = exchange(client, token)

    assert response.status_code == 502
    assert (
        seed_scan(response.text, SEED_CODE, SEED_PROVIDER_BODY, HONEYPOT_STRING_TWO)
        == []
    )

    out = capsys.readouterr()
    assert (
        seed_scan(out.out, SEED_CODE, SEED_PROVIDER_BODY, HONEYPOT_STRING_TWO) == []
    )
    assert (
        seed_scan(out.err, SEED_CODE, SEED_PROVIDER_BODY, HONEYPOT_STRING_TWO) == []
    )
    for record in caplog.records:
        message = record.getMessage()
        assert (
            seed_scan(message, SEED_CODE, SEED_PROVIDER_BODY, HONEYPOT_STRING_TWO) == []
        )


# --- VAL-SECU-019: redirect_uri must match the explicit allowlist, before Notion ---


def test_redirect_uri_allowlist_accepts_only_the_two_registered_uris(tmp_path):
    calls = {"count": 0}

    def fake_exchange(code, redirect_uri):
        calls["count"] += 1
        return {"access_token": HONEYPOT_STRING_ONE, "bot_id": "b"}

    client, token = make_exchange_client(tmp_path, fake_exchange)

    for uri in ALLOWED:
        response = exchange(client, token, redirect_uri=uri)
        assert response.status_code == 200, repr(uri)
    assert calls["count"] == 2

    for uri in DISALLOWED:
        response = exchange(client, token, redirect_uri=uri)
        assert response.status_code == 400, repr(uri)
        assert response.json()["detail"] == "redirect_uri is not allowlisted"
    assert calls["count"] == 2  # Notion was never contacted for rejected URIs


def test_disallowed_redirect_uri_400_even_when_oauth_is_unconfigured(tmp_path):
    client, token = make_exchange_client(tmp_path, None)  # no Notion config
    response = exchange(client, token, redirect_uri="https://evil.example/callback")
    assert response.status_code == 400
