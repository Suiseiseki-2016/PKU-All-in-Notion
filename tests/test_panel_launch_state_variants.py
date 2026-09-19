"""Browser-startable fake variants for the honest sync-state matrix."""

from __future__ import annotations

from fastapi.testclient import TestClient

from pku_sync.panel.directory import STATE_EMPTY, STATE_ERROR, SYNC_ERROR_UNAVAILABLE
from pku_sync.panel.fake_directory import build_fake_directory
from pku_sync.panel.webapi import create_app


def client_for(variant: str) -> TestClient:
    return TestClient(create_app(directory_service=build_fake_directory(variant=variant)))


def test_empty_variant_is_successfully_empty_with_no_fake_rows():
    client = client_for("empty")
    response = client.get("/api/directory")
    assert response.status_code == 200
    assert response.json()["courses"] == []
    assert response.json()["sync"]["state"] == STATE_EMPTY
    assert client.get("/api/sync/state").json()["state"] == STATE_EMPTY


def test_usage_cap_variant_is_an_error_never_empty_or_done():
    client = client_for("usage-cap")
    response = client.get("/api/directory")
    assert response.status_code == 503
    assert response.json() == {"detail": SYNC_ERROR_UNAVAILABLE}
    assert client.get("/api/sync/state").json() == {
        "state": STATE_ERROR,
        "last_sync_at": None,
        "error_reason": SYNC_ERROR_UNAVAILABLE,
    }
