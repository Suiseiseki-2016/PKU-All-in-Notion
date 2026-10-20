"""Cloud retries must renew consumed direct-upload authorizations."""
import json

import httpx
import pytest

from pku_sync import transcription


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(transcription, "_RETRY_DELAY", 0)


def mock_client(monkeypatch, handler):
    client_type = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client_type(
        transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize("stage,status", [
    ("complete", 408), ("complete", 409), ("complete", 429), ("complete", 500),
    ("complete", 502), ("complete", 503), ("complete", 504), ("complete", 520),
    ("complete", 522), ("complete", 524), ("put", 500), ("put", 503),
    ("init", None), ("put", None), ("complete", None),
])
def test_direct_retry_renews_entire_upload_cycle(tmp_path, monkeypatch, stage, status):
    audio = tmp_path / "part-0002.m4a"
    audio.write_bytes(b"audio")
    events, ids = [], []

    def handle(request):
        current = "init" if request.url.path.endswith("/init") else (
            "put" if request.method == "PUT" else "complete")
        events.append(current)
        if current == "init":
            ids.append(f"{len(ids) + 1:032d}")
            assert json.loads(request.content) == {
                "size_bytes": 5, "source_id": "lecture", "part_index": 2, "transcribe_mode": "basic"}
        if current == "put":
            assert request.read() == b"audio"
            assert "authorization" not in request.headers
        if current == "complete":
            assert json.loads(request.content) == {"upload_id": ids[-1]}
        if len(ids) == 1 and current == stage:
            if status is None:
                raise httpx.ReadError("connection reset", request=request)
            return httpx.Response(status)
        if current == "init":
            return httpx.Response(200, json={"upload_id": ids[-1],
                "upload_url": f"https://private.oss-cn-beijing.aliyuncs.com/{ids[-1]}"})
        return httpx.Response(200, json={"segments": []})

    mock_client(monkeypatch, handle)
    assert transcription._upload_direct("https://relay.example/v1/transcribe", "token", audio,
        source_id="lecture", transcribe_mode="basic") == {"segments": []}
    assert events == ["init", "put", "complete"][:["init", "put", "complete"].index(stage) + 1] + ["init", "put", "complete"]
    assert len(set(ids)) == 2


@pytest.mark.parametrize("stage", ["init", "put", "complete"])
@pytest.mark.parametrize("status", [401, 402, 403, 413])
def test_direct_auth_and_quota_errors_are_not_retried(tmp_path, monkeypatch, stage, status):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    events = []

    def handle(request):
        current = "put" if request.method == "PUT" else request.url.path.rsplit("/", 1)[-1]
        events.append(current)
        if current == stage:
            return httpx.Response(status)
        return httpx.Response(200, json={"upload_id": "a" * 32,
            "upload_url": "https://private.oss-cn-beijing.aliyuncs.com/audio"})

    mock_client(monkeypatch, handle)
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        transcription._upload_direct("https://relay.example/v1/transcribe", "token", audio)
    assert events.count("init") == 1


def test_direct_persistent_upstream_failure_stops_after_two_cycles(tmp_path, monkeypatch):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    events = []

    def handle(request):
        events.append(request.method)
        if request.url.path.endswith("/init"):
            return httpx.Response(200, json={"upload_id": "a" * 32,
                "upload_url": "https://private.oss-cn-beijing.aliyuncs.com/audio"})
        return httpx.Response(200 if request.method == "PUT" else 502)

    mock_client(monkeypatch, handle)
    with pytest.raises(RuntimeError, match="HTTP 502"):
        transcription._upload_direct("https://relay.example/v1/transcribe", "token", audio)
    assert events == ["POST", "PUT", "POST"] * 2


@pytest.mark.parametrize("status", [401, 402, 403, 413, 429, 502, 503, 504])
def test_relay_upload_retries_only_transient_upstream_statuses(tmp_path, monkeypatch, status):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    calls = []

    def handle(request):
        calls.append(request.read())
        assert b"audio" in calls[-1]
        return httpx.Response(status if len(calls) == 1 else 200, json={"segments": []})

    mock_client(monkeypatch, handle)
    if status in (502, 503, 504):
        assert transcription._upload("https://relay.example/v1/transcribe", "token", audio) == {"segments": []}
        assert len(calls) == 2
    else:
        with pytest.raises(RuntimeError, match=f"HTTP {status}"):
            transcription._upload("https://relay.example/v1/transcribe", "token", audio)
        assert len(calls) == 1


@pytest.mark.parametrize("suffix", ["；已完成片段会在重试时复用。", "；已完成片段会在重试时复用", "；已完成片段会在重试时复用。。"])
def test_part_error_has_only_one_reuse_reminder(tmp_path, suffix):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    def fail(*args):
        raise RuntimeError("云端直传转写失败（HTTP 502）" + suffix)

    with pytest.raises(RuntimeError) as caught:
        transcription._transcribe_parts([(tmp_path / "audio.m4a", 0)],
            "https://relay.example", "token", video, tmp_path / "transcript.json", uploader=fail)
    assert str(caught.value) == "第 1/1 段转写失败；已完成片段会在重试时复用：云端直传转写失败（HTTP 502）"
