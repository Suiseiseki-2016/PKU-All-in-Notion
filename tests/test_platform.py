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


@pytest.mark.parametrize("detail,message", [
    ("AI model not enabled for this account", "当前账号尚未开通所选 AI 模型，请切换模型或联系管理员。"),
    ("email verification required", "请先完成邮箱验证后再使用转写、AI 或兑换额度"),
])
def test_llm_forbidden_error_distinguishes_model_access_from_email_verification(detail, message):
    error = platform._llm_error(httpx.Response(403, json={"detail": detail}))
    assert str(error) == message
    assert error.status_code == 403


@pytest.mark.parametrize("model", ["qwen3.5-flash", "qwen3.7-plus", "qwen3.8-max-0902", "qwen3.8-max"])
def test_notes_explicit_cloud_model_binds_payload_key_and_lost_enqueue(monkeypatch, tmp_path, model):
    s = settings(tmp_path)
    s.platform_token = "opaque-token"
    s.notes_model = model
    model = "qwen3.8-max-0902" if model == "qwen3.8-max" else model
    sent = []
    job_id = "f" * 32
    def post(url, **kwargs):
        sent.append(kwargs["json"])
        if len(sent) == 1:
            raise httpx.ReadTimeout("accepted response lost")
        return httpx.Response(202, json={"job_id": job_id, "status": "queued"})
    monkeypatch.setattr(platform, "_post", post)
    monkeypatch.setattr(platform, "_get", lambda *a, **kw: httpx.Response(200,
        json={"job_id": job_id, "status": "succeeded", "content": "候选", "points_charged": .01}))
    monkeypatch.setattr(platform.time, "sleep", lambda _: None)
    assert platform.llm("notes", "原文", s, system="逐句核验")["content"] == "候选"
    assert len(sent) == 2 and sent[0] == sent[1]
    assert sent[0]["model"] == model
    assert sent[0]["idempotency_key"] == platform._llm_job_key("notes", "逐句核验", "原文", model=model)
    assert sent[0]["idempotency_key"] != platform._llm_job_key("notes", "逐句核验", "原文")
    assert platform._llm_payload_hash("notes", "逐句核验", "原文", model=model) != platform._llm_payload_hash("notes", "逐句核验", "原文")


def test_notes_model_failures_keep_attempt_state_in_exact_model_namespace(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "opaque-token"
    sent = []
    monkeypatch.setattr(platform, "_post", lambda *a, **kw: (sent.append(kw["json"]),
        httpx.Response(202, json={"job_id": "e" * 32}))[1])
    monkeypatch.setattr(platform, "_get", lambda *a, **kw: httpx.Response(200,
        json={"job_id": "e" * 32, "status": "failed", "error_status": 402}))
    for model in ("qwen3.7-plus", "qwen3.5-flash", "qwen3.7-plus"):
        s.notes_model = model
        with pytest.raises(platform.PlatformError):
            platform.llm("notes", "原文", s, system="核验")
    assert len({row["idempotency_key"] for row in sent}) == 3
    for model, attempt in (("qwen3.7-plus", 2), ("qwen3.5-flash", 1)):
        digest = platform._llm_payload_hash("notes", "核验", "原文", model=model)
        assert json.loads((tmp_path / ".llm-jobs" / f"{digest}.json").read_text("utf8")) == {"attempt": attempt}
    assert not (tmp_path / ".llm-jobs" / f'{platform._llm_payload_hash("notes", "核验", "原文")}.json').exists()


@pytest.mark.parametrize("model", ["qwen/unknown", " qwen3.7-plus", 42])
def test_notes_unknown_explicit_cloud_model_rejected_before_any_post_or_state(monkeypatch, tmp_path, model):
    s = settings(tmp_path)
    s.platform_token = "opaque-token"
    s.notes_model = model
    monkeypatch.setattr(platform, "_post", lambda *a, **kw: pytest.fail("invalid model submitted"))
    monkeypatch.setattr(platform, "_llm_job_state_path", lambda *a, **kw: pytest.fail("invalid model touched state"))
    with pytest.raises(platform.PlatformError, match="模型"):
        platform.llm("notes", "原文", s)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("model", ["qwen3.5-flash", "qwen3.7-plus", "qwen3.8-max-0902", "qwen3.8-max"])
def test_notes_budget_is_full_system_and_user_before_any_post_or_state(monkeypatch, tmp_path, model):
    s = settings(tmp_path)
    s.platform_token = "opaque-token"
    s.notes_model = model
    monkeypatch.setattr(platform, "_post", lambda *a, **kw: pytest.fail("oversize model submitted"))
    monkeypatch.setattr(platform, "_llm_job_state_path", lambda *a, **kw: pytest.fail("oversize model touched state"))
    with pytest.raises(platform.PlatformError, match="200000"):
        platform.llm("notes", "a" * 199999, s, system="系统")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("model", [None, "", "qwen/qwen3.5-397b-a17b"])
def test_notes_legacy_model_keeps_original_wire_and_keys(monkeypatch, tmp_path, model):
    import hashlib
    s = settings(tmp_path)
    s.platform_token = "opaque-token"
    s.notes_model = model
    legacy = hashlib.sha256(json.dumps({"operation": "notes", "system": "核验", "user": "原文"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key = hashlib.sha256(f"{legacy}:0".encode("ascii")).hexdigest()[:32]
    sent = []
    monkeypatch.setattr(platform, "_post", lambda *a, **kw: (sent.append(kw["json"]),
        httpx.Response(202, json={"job_id": "e" * 32}))[1])
    monkeypatch.setattr(platform, "_get", lambda *a, **kw: httpx.Response(200,
        json={"job_id": "e" * 32, "status": "succeeded", "content": "原正文", "points_charged": .01}))
    assert platform.llm("notes", "原文", s, system="核验")["content"] == "原正文"
    assert sent == [{"operation": "notes", "system": "核验", "user": "原文", "idempotency_key": key}]
    assert platform._llm_payload_hash("notes", "核验", "原文", model=None) == legacy
    assert platform._llm_job_key("notes", "核验", "原文", model=None) == key


def test_notes_plus_exact_budget_and_other_operations_keep_legacy_contract(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = "opaque-token"
    s.notes_model = "qwen3.7-plus"
    sent = []
    monkeypatch.setattr(platform, "_post", lambda *a, **kw: (sent.append(kw["json"]),
        httpx.Response(202, json={"job_id": "e" * 32}))[1])
    monkeypatch.setattr(platform, "_get", lambda *a, **kw: httpx.Response(200,
        json={"job_id": "e" * 32, "status": "succeeded", "content": "候选", "points_charged": .01}))
    platform.llm("notes", "a" * 199999, s, system="核")
    assert sent[-1]["model"] == "qwen3.7-plus"
    s.notes_model = "unknown"
    platform.llm("quiz", "题目", s, system="核验")
    assert sent[-1] == {"operation": "quiz", "system": "核验", "user": "题目",
                       "idempotency_key": platform._llm_job_key("quiz", "核验", "题目")}


def test_qa_job_lost_enqueue_preserves_context_and_existing_key(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = 'opaque-token'
    context = {'purpose': 'assignment-requirements', 'course_id': '_101_1', 'content_id': '_42_1'}
    posted = []
    job_id = 'd' * 32
    def post(url, **kwargs):
        posted.append(kwargs['json'])
        assert url.endswith('/v1/llm/jobs')
        if len(posted) == 1:
            raise httpx.ReadTimeout('accepted response lost')
        return httpx.Response(202, json={'job_id': job_id, 'status': 'queued'})
    monkeypatch.setattr(platform, '_post', post)
    monkeypatch.setattr(platform, '_get', lambda *args, **kwargs: httpx.Response(200,
        json={'job_id': job_id, 'status': 'succeeded', 'content': 'result', 'points_charged': .01}))
    monkeypatch.setattr(platform.time, 'sleep', lambda _: None)
    assert platform.llm('qa', 'official requirement', s, system='extract', operation_context=context)['content'] == 'result'
    assert len(posted) == 2 and posted[0] == posted[1]
    assert posted[0]['operation_context'] == context
    assert posted[0]['idempotency_key'] == platform._llm_job_key('qa', 'extract', 'official requirement')


def test_secret_or_unbounded_operation_context_never_sent(monkeypatch, tmp_path):
    s = settings(tmp_path)
    s.platform_token = 'opaque-token'
    monkeypatch.setattr(platform, '_post', lambda *args, **kwargs: pytest.fail('invalid context sent'))
    for context in ({'token': 'secret'}, {'source_id': 'https://signed.example?a=secret'}, {'purpose': 'a' * 161}):
        with pytest.raises(platform.PlatformError, match='源身份无效'):
            platform.llm('quiz', 'prompt', s, operation_context=context)


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


@pytest.mark.parametrize('method', ['POST', 'PUT'])
@pytest.mark.parametrize('error', [httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ConnectError])
def test_write_transport_error_does_not_implicitly_repeat_mutation(monkeypatch, method, error):
    sent = []
    failure = error('write outcome is unknown')
    def write(url, **kwargs):
        sent.append((method, url, kwargs))
        raise failure
    monkeypatch.setattr(platform.httpx, method.lower(), write)
    class DirectClient:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def request(self, method, url, **kwargs):
            sent.append((method, url, kwargs))
            return httpx.Response(200, json={'ok': True})
    monkeypatch.setattr(platform.httpx, 'Client', DirectClient)
    with pytest.raises(error) as caught:
        platform._request(method, 'https://relay.example/v1/catalog/_123_1/observations', json={})
    assert caught.value is failure
    assert len(sent) == 1


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

    assert platform.sync_account_profile(s)["status"] == "current"
    assert sent == {}
    assert json.loads((tmp_path / "notion-learning-home.json").read_text("utf-8")) == home
    encoded = json.dumps(sent)
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

    assert platform.sync_account_profile(s) == {
        "available": True,
        "status": "current",
        "revision": 7,
    }
    assert json.loads(path.read_text("utf-8")) == local
    assert not (tmp_path / "notion-learning-home.pre-account-sync.json").exists()


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

    assert platform.sync_account_profile(s)["status"] == "current"
    assert json.loads(path.read_text("utf-8")) == local
