"""Account snapshot contract tests; no network or paid services."""
from types import SimpleNamespace

import httpx
import pytest

from pku_sync import catalog_snapshots as catalog
from pku_sync import platform


def snapshot(count=5):
    return {
        "schema": 1, "authority": "course.pku.edu.cn", "course_id": "_101545_1",
        "course": {"course_id": "_101545_1", "name": "发展心理学", "code": "", "term": ""},
        "sections": {
            "recordings": {"complete": True, "items": [
                {"source_id": f"recording:{i}", "title": "发展心理学", "recorded_at": f"2026-09-{1+i*7:02} 13:00:00"}
                for i in range(count)]},
            "materials": {"complete": True, "items": []},
            "assignments": {"complete": True, "items": []},
            "announcements": {"complete": True, "items": []},
        },
    }


def settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, platform_token="mock-account", cloud_transcribe_url="https://relay.example/v1/transcribe")


def test_snapshot_rejects_partial_sections_and_duplicate_source_ids():
    partial = snapshot()
    partial["sections"]["recordings"]["complete"] = False
    with pytest.raises(ValueError):
        catalog.validate_snapshot(partial)
    duplicate = snapshot()
    duplicate["sections"]["recordings"]["items"][1]["source_id"] = "recording:0"
    with pytest.raises(ValueError):
        catalog.validate_snapshot(duplicate)


def test_recording_source_id_survives_folder_title_and_token_changes():
    from pku_sync.models import Recording
    original = Recording(course_id="_101545_1", recorded_at="2026-09-29 13:00:00", title="第一讲", play_url="playVideo.action?token=old")
    newer = original.model_copy(update={"title": "改名", "play_url": "playVideo.action?token=new"})
    assert catalog.recording_source_id(original) == catalog.recording_source_id(newer)
    assert catalog.recording_source_id(original) != catalog.recording_source_id(original.model_copy(update={"course_id": "other"}))


def test_server_conflict_is_not_rebased_with_stale_local_data(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(platform, "_put", lambda url, **kw: (calls.append(kw), httpx.Response(409, json={}))[1])
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.publish(settings(tmp_path), "_101545_1", {"revision": 7, "observation_id": "ticket"}, snapshot(4))
    assert len(calls) == 1
    assert calls[0]["json"]["revision"] == 7


@pytest.mark.parametrize("status", [401, 404, 503])
def test_unavailable_account_service_never_grants_shared_write(tmp_path, monkeypatch, status):
    monkeypatch.setattr(platform, "_get", lambda *a, **kw: httpx.Response(status, json={}))
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.current(settings(tmp_path), "_101545_1")


@pytest.mark.parametrize('status', [402, 403, 409, 429, 503, 200])
def test_unavailable_or_unreadable_catalog_has_cache_preservation_and_action(tmp_path, monkeypatch, status):
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(kwargs)
        return httpx.Response(status, content=b'not-json-secret-provider-content')
    monkeypatch.setattr(platform, '_get', unavailable)
    monkeypatch.setattr(platform, '_post', lambda *a, **kw: pytest.fail('read failure cannot acquire or replay'))
    monkeypatch.setattr(platform, '_put', lambda *a, **kw: pytest.fail('read failure cannot publish or rebase'))
    with pytest.raises(catalog.CatalogUnavailable) as error:
        catalog.current(settings(tmp_path), '_101545_1')
    message = str(error.value)
    assert any(word in message for word in ('重试', '检查', '重新', '稍后'))
    assert any(word in message for word in ('保留', '只读'))
    assert 'secret-provider-content' not in message and len(calls) == 1


@pytest.mark.parametrize('detail,expected', [
    ('another shared projection is running', '上次共享目录写入许可尚未释放'),
    ('shared projection in progress', '上次共享目录写入许可尚未释放'),
    ('stale projection revision', '课程目录版本已变化'),
    ('stale course revision; do not rebase cached data', '课程目录版本已变化'),
])
def test_known_conflicts_explain_safe_next_step_without_rebasing(tmp_path, monkeypatch, detail, expected):
    calls = []
    def blocked(*args, **kwargs):
        calls.append(kwargs)
        return httpx.Response(409, json={'detail': detail})
    monkeypatch.setattr(platform, '_post', blocked)
    with pytest.raises(catalog.CatalogUnavailable, match=expected):
        catalog._call(settings(tmp_path), 'POST', '_101545_1', '/projection', {'revision': 7})
    assert len(calls) == 1 and calls[0]['json']['revision'] == 7


def test_conflict_does_not_echo_untrusted_server_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, '_post', lambda *a, **kw: httpx.Response(409, json={'detail': 'secret-token-and-student-text'}))
    with pytest.raises(catalog.CatalogUnavailable) as error:
        catalog._call(settings(tmp_path), 'POST', '_101545_1', '/projection', {'revision': 7})
    assert 'secret-token' not in str(error.value)


def test_only_explicit_unpublished_course_is_omitted_from_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "_call", lambda *a, **kw: {"revision": 0, "snapshot": None})
    assert catalog.published_current(settings(tmp_path), "_101545_1") is None
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.current(settings(tmp_path), "_101545_1")


@pytest.mark.parametrize("value", [{"revision": 0}, {"revision": False, "snapshot": None},
    {"revision": "0", "snapshot": None}, {"revision": -1, "snapshot": None},
    {"snapshot": None}, {"revision": 1, "snapshot": None},
    {"revision": 0, "snapshot": snapshot()}, {"revision": 1, "snapshot": {}}])
def test_batch_discovery_does_not_turn_malformed_envelopes_into_absence(tmp_path, monkeypatch, value):
    monkeypatch.setattr(catalog, "_call", lambda *a, **kw: value)
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.published_current(settings(tmp_path), "_101545_1")


def test_non_increasing_or_mismatched_publish_response_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "_put", lambda *a, **kw: httpx.Response(200, json={"revision": 7, "snapshot": snapshot(4)}))
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.publish(settings(tmp_path), "_101545_1", {"revision": 7, "observation_id": "ticket"}, snapshot())


def test_small_metadata_upload_never_contains_media_addresses_or_credentials(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(platform, "_put", lambda *a, **kw: (calls.append(kw["json"]), httpx.Response(200, json={"revision": 8, "snapshot": kw["json"]["snapshot"]}))[1])
    result = catalog.publish(settings(tmp_path), "_101545_1", {"revision": 7, "observation_id": "ticket"}, snapshot())
    assert result["revision"] == 8
    assert "mock-account" not in str(calls)
    unsafe = snapshot()
    unsafe["sections"]["recordings"]["items"][0]["mp4_url"] = "https://private.example/video"
    with pytest.raises(ValueError):
        catalog.validate_snapshot(unsafe)


def test_readback_mismatch_after_upload_does_not_grant_projection(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "_get", lambda *a, **kw: httpx.Response(200, json={"revision": 8, "snapshot": snapshot(4)}))
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.verify_current(settings(tmp_path), {"revision": 8, "snapshot": snapshot()})


def test_switching_account_cannot_consume_previous_accounts_canonical_cache(tmp_path):
    first = settings(tmp_path)
    catalog.activate_account(first)
    catalog.save_cache(tmp_path, {"revision": 1, "snapshot": snapshot()})
    assert catalog.cached(tmp_path, "_101545_1")["revision"] == 1
    other = settings(tmp_path)
    other.platform_token = "other-account"
    catalog.activate_account(other)
    assert catalog.cached(tmp_path, "_101545_1") is None
    catalog.activate_account(first)
    assert catalog.cached(tmp_path, "_101545_1")["revision"] == 1
