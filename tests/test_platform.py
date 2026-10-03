from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from pku_sync import platform


def settings(tmp_path):
    return SimpleNamespace(
        cloud_transcribe_url="https://pku.aeoluswu.info/v1/transcribe",
        platform_token="",
        transcription_backend="local",
        data_dir=tmp_path,
        llm_timeout=600,
    )


def test_activate_writes_only_local_platform_settings(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setattr(platform, "env_file_path", lambda _: tmp_path / ".env")
    monkeypatch.setattr(
        platform.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            200, json={"platform_token": "opaque-token", "transcribe_seconds_remaining": 900}
        ),
    )

    result = platform.activate(" CODE123 ", s)

    assert result == {"transcribe_seconds_remaining": 900}
    assert s.platform_token == "opaque-token"
    assert s.transcription_backend == "cloud"
    text = (tmp_path / ".env").read_text("utf-8")
    assert "PLATFORM_TOKEN=opaque-token" in text
    assert "TRANSCRIPTION_BACKEND=cloud" in text


def test_quota_does_not_expose_platform_token(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "opaque-token"
    seen = {}

    def fake_get(url, headers, timeout):
        seen.update(headers)
        return httpx.Response(200, json={"transcribe_seconds_remaining": 3600})

    monkeypatch.setattr(platform.httpx, "get", fake_get)
    assert platform.quota(s) == {
        "active": True,
        "available": True,
        "transcribe_seconds_remaining": 3600,
    }
    assert seen["Authorization"] == "Bearer opaque-token"


def test_cloud_request_retries_direct_when_proxy_transport_fails(monkeypatch):
    monkeypatch.setattr(
        platform.httpx,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("proxy TLS")),
    )
    seen = {}

    class DirectClient:
        def __init__(self, *, trust_env):
            seen["trust_env"] = trust_env

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, **kwargs):
            seen.update({"method": method, "url": url})
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(platform.httpx, "Client", DirectClient)

    response = platform._get("https://relay.example/v1/profile", timeout=15)

    assert response.status_code == 200
    assert seen == {
        "trust_env": False,
        "method": "GET",
        "url": "https://relay.example/v1/profile",
    }


def test_notes_job_retries_lost_enqueue_with_same_idempotency_key(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "opaque-token"
    posted = []
    job_id = "a" * 32

    def post(url, **kwargs):
        posted.append((url, kwargs))
        if len(posted) == 1:
            raise httpx.ReadTimeout("enqueue response lost")
        return httpx.Response(202, json={"job_id": job_id, "status": "queued"})

    monkeypatch.setattr(platform, "_post", post)
    monkeypatch.setattr(platform, "_get", lambda *a, **kw: httpx.Response(
        200, json={"job_id": job_id, "status": "succeeded", "content": "笔记", "points_charged": 0.1}
    ))
    monkeypatch.setattr(platform.time, "sleep", lambda _: None)

    assert platform.llm("notes", "课堂原文", s, system="核对术语") == {
        "content": "笔记", "points_charged": 0.1,
    }
    assert len(posted) == 2
    assert all(url.endswith("/v1/llm/jobs") for url, _ in posted)
    assert posted[0][1]["json"] == posted[1][1]["json"]
    key = posted[0][1]["json"]["idempotency_key"]
    assert len(key) == 32 and set(key) <= set("0123456789abcdef")
    assert posted[0][1]["headers"] == {"Authorization": "Bearer opaque-token"}
    assert platform._llm_job_key("notes", "核对术语", "课堂原文") == key
    assert platform._llm_job_key("notes", "核对术语", "另一段原文") != key


def test_notes_job_polls_until_success(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "token"
    job_id = "b" * 32
    replies = iter([
        httpx.Response(200, json={"job_id": job_id, "status": "queued"}),
        httpx.Response(200, json={"job_id": job_id, "status": "running"}),
        httpx.Response(200, json={"job_id": job_id, "status": "succeeded", "content": "章节", "points_charged": 2}),
    ])
    calls = []
    monkeypatch.setattr(platform, "_post", lambda *a, **kw: httpx.Response(202, json={"job_id": job_id, "status": "queued"}))
    monkeypatch.setattr(platform, "_get", lambda url, **kw: (calls.append((url, kw)), next(replies))[1])
    monkeypatch.setattr(platform.time, "sleep", lambda _: None)

    assert platform.llm("notes", "原文", s) == {"content": "章节", "points_charged": 2}
    assert len(calls) == 3
    assert all(url.endswith("/v1/llm/jobs/" + job_id) for url, _ in calls)


def test_notes_job_failed_maps_safe_error_and_status(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "token"
    job_id = "c" * 32
    posted = []
    monkeypatch.setattr(platform, "_post", lambda *a, **kw: (posted.append(kw["json"]["idempotency_key"]), httpx.Response(202, json={"job_id": job_id, "status": "queued"}))[1])
    monkeypatch.setattr(platform, "_get", lambda *a, **kw: httpx.Response(
        200, json={"job_id": job_id, "status": "failed", "error_status": 402, "error": "private upstream detail"}
    ))

    with pytest.raises(platform.PlatformError, match="AI 点不足") as exc:
        platform.llm("notes", "原文", s)
    assert exc.value.status_code == 402
    assert "private upstream detail" not in str(exc.value)
    # A terminal failed job keeps its status on the server. A later user retry
    # must get a new key, whereas connection loss or timeout keeps the old key.
    with pytest.raises(platform.PlatformError, match="AI 点不足"):
        platform.llm("notes", "原文", s)
    assert posted[0] != posted[1]
    digest = platform._llm_payload_hash("notes", platform._DEFAULT_LLM_SYSTEM, "原文")
    assert (tmp_path / ".llm-jobs" / f"{digest}.json").read_text("utf-8") == '{"attempt": 2}'


def test_notes_job_rejects_bad_response(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "token"
    monkeypatch.setattr(platform, "_post", lambda *a, **kw: httpx.Response(202, json={"job_id": "bad", "status": "queued"}))
    with pytest.raises(platform.PlatformError, match="云端返回异常"):
        platform.llm("notes", "原文", s)


def test_non_notes_keeps_synchronous_llm_api(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "token"
    s.llm_timeout = 20
    called = []
    monkeypatch.setattr(platform, "_post", lambda url, **kw: (called.append(url), httpx.Response(
        200, json={"content": "结果", "points_charged": 0.01}
    ))[1])
    assert platform.llm("summary", "问题", s) == {"content": "结果", "points_charged": 0.01}
    assert called == ["https://pku.aeoluswu.info/v1/llm"]


def test_notes_timeout_respects_config_and_preserves_retry_key(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "token"
    s.llm_timeout = 40
    job_id = "d" * 32
    keys = []
    clock = [0.0]
    monkeypatch.setattr(platform.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(platform.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(platform, "_post", lambda *a, **kw: (keys.append(kw["json"]["idempotency_key"]), httpx.Response(202, json={"job_id": job_id, "status": "queued"}))[1])
    monkeypatch.setattr(platform, "_get", lambda *a, **kw: httpx.Response(200, json={"job_id": job_id, "status": "running"}))

    with pytest.raises(platform.PlatformError, match="仍在云端进行"):
        platform.llm("notes", "原文", s)
    assert clock[0] == 40
    with pytest.raises(platform.PlatformError, match="仍在云端进行"):
        platform.llm("notes", "原文", s)
    assert keys[0] == keys[1]


def test_profile_bootstraps_remote_from_safe_local_home(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "account-session"
    s.notion_token = "local-notion-secret"
    s.pku_password = "local-pku-secret"
    home = {
        "id": "3e591b6f-53e1-81b5-9320-c73c5f64ee6e",
        "url": "https://app.notion.com/p/PKU-All-in-Notion-3e591b6f53e181b59320c73c5f64ee6e",
        "parent_id": "32091b6f-53e1-80fc-8ec8-e650b4479f5d",
    }
    (tmp_path / "notion-learning-home.json").write_text(
        json.dumps(home), encoding="utf-8"
    )
    sent = {}
    monkeypatch.setattr(
        platform,
        "_get",
        lambda *args, **kwargs: httpx.Response(
            200, json={"revision": 0, "profile": {}, "updated_at": None}
        ),
    )

    def fake_put(url, **kwargs):
        sent.update(kwargs)
        return httpx.Response(200, json={"revision": 1, "profile": kwargs["json"]["profile"]})

    monkeypatch.setattr(platform, "_put", fake_put)

    assert platform.sync_account_profile(s)["status"] == "pushed"
    assert sent["json"] == {"revision": 0, "profile": {"notion_home": home}}
    encoded = json.dumps(sent["json"])
    assert "local-notion-secret" not in encoded
    assert "local-pku-secret" not in encoded
    assert "account-session" not in encoded


def test_profile_pulls_accessible_remote_home_and_keeps_backup(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "account-session"
    s.notion_token = "notion-token"
    local = {
        "id": "11111111-1111-1111-1111-111111111111",
        "url": "https://www.notion.so/old",
        "parent_id": "22222222-2222-2222-2222-222222222222",
    }
    remote = {
        "id": "3e591b6f-53e1-81b5-9320-c73c5f64ee6e",
        "url": "https://app.notion.com/p/new",
        "parent_id": "32091b6f-53e1-80fc-8ec8-e650b4479f5d",
    }
    path = tmp_path / "notion-learning-home.json"
    path.write_text(json.dumps(local), encoding="utf-8")
    monkeypatch.setattr(
        platform,
        "_get",
        lambda *args, **kwargs: httpx.Response(
            200, json={"revision": 7, "profile": {"notion_home": remote}}
        ),
    )
    monkeypatch.setattr(platform, "_notion_home_accessible", lambda *args: True)

    assert platform.sync_account_profile(s) == {
        "available": True,
        "status": "pulled",
        "revision": 7,
    }
    assert json.loads(path.read_text("utf-8")) == remote
    backup = tmp_path / "notion-learning-home.pre-account-sync.json"
    assert json.loads(backup.read_text("utf-8")) == local


def test_profile_does_not_replace_home_without_notion_access(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "account-session"
    local = {
        "id": "11111111-1111-1111-1111-111111111111",
        "url": "https://www.notion.so/old",
        "parent_id": "22222222-2222-2222-2222-222222222222",
    }
    path = tmp_path / "notion-learning-home.json"
    path.write_text(json.dumps(local), encoding="utf-8")
    monkeypatch.setattr(
        platform,
        "_get",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={
                "revision": 2,
                "profile": {
                    "notion_home": {
                        "id": "33333333-3333-3333-3333-333333333333",
                        "url": "https://www.notion.so/new",
                        "parent_id": "44444444-4444-4444-4444-444444444444",
                    }
                },
            },
        ),
    )

    assert platform.sync_account_profile(s)["status"] == "notion_reconnect_required"
    assert json.loads(path.read_text("utf-8")) == local
