"""Unit tests for the transcription backend dispatch (no real whisper)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pku_sync import transcription


def make_settings(backend: str, **extra) -> SimpleNamespace:
    defaults = {
        "transcription_backend": backend,
        "cloud_transcribe_url": "",
        "platform_token": "",
    }
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def test_local_backend_dispatches_to_media_transcribe(monkeypatch, tmp_path):
    cfg = make_settings("local")
    calls = {}

    def fake_media_transcribe(video, target, config):
        calls["args"] = (video, target, config)
        return {"segments": []}

    monkeypatch.setattr("pku_sync.media.transcribe", fake_media_transcribe)
    result = transcription.transcribe(tmp_path / "video.mp4", tmp_path / "t.json", cfg)
    assert result == {"segments": []}
    assert calls["args"] == (tmp_path / "video.mp4", tmp_path / "t.json", cfg)


def test_backend_name_is_normalized(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        "pku_sync.media.transcribe", lambda video, target, cfg: seen.append(1) or {}
    )
    transcription.transcribe(
        tmp_path / "v.mp4", tmp_path / "t.json", make_settings("  Local  ")
    )
    assert seen == [1]


def test_empty_backend_falls_back_to_local(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        "pku_sync.media.transcribe", lambda video, target, cfg: seen.append(1) or {}
    )
    transcription.transcribe(
        tmp_path / "v.mp4", tmp_path / "t.json", make_settings("")
    )
    assert seen == [1]


def test_cloud_requires_relay_url(tmp_path):
    # Until the M2 relay is deployed and configured, the cloud backend still
    # refuses loudly instead of half-working (docs/SERVICE_PLAN.md §3.3).
    with pytest.raises(RuntimeError, match="CLOUD_TRANSCRIBE_URL"):
        transcription.transcribe(
            tmp_path / "v.mp4", tmp_path / "t.json", make_settings("cloud")
        )


def test_cloud_requires_platform_token(tmp_path):
    cfg = make_settings("cloud", cloud_transcribe_url="https://relay.example/v1/transcribe")
    with pytest.raises(RuntimeError, match="PLATFORM_TOKEN"):
        transcription.transcribe(tmp_path / "v.mp4", tmp_path / "t.json", cfg)


def test_cloud_uploads_audio_only_and_writes_target(tmp_path, monkeypatch):
    cfg = make_settings(
        "cloud",
        cloud_transcribe_url="https://relay.example/v1/transcribe",
        platform_token="m-key",
    )
    calls = {}

    def fake_extract(video, out):
        calls["extract"] = (video, Path(out))
        Path(out).write_bytes(b"fake-audio")
        return Path(out)

    def fake_upload(url, key, audio):
        calls["upload"] = (url, key, Path(audio))
        assert Path(audio).read_bytes() == b"fake-audio"
        return {"language": "zh", "segments": [{"start": 0.0, "text": "第一讲"}]}

    monkeypatch.setattr(transcription, "_extract_audio", fake_extract)
    monkeypatch.setattr(transcription, "_upload", fake_upload)
    result = transcription.transcribe(tmp_path / "v.mp4", tmp_path / "t.json", cfg)

    assert result["segments"][0]["text"] == "第一讲"
    # §3.2: only the extracted audio track is uploaded, never the video.
    assert calls["extract"][0] == tmp_path / "v.mp4"
    assert calls["upload"][2].suffix == ".m4a"
    assert calls["upload"][2] != tmp_path / "v.mp4"
    assert calls["upload"][:2] == ("https://relay.example/v1/transcribe", "m-key")
    written = json.loads((tmp_path / "t.json").read_text("utf-8"))
    assert written == result


def test_cloud_missing_ffmpeg_is_loud(tmp_path, monkeypatch):
    cfg = make_settings(
        "cloud",
        cloud_transcribe_url="https://relay.example/v1/transcribe",
        platform_token="m-key",
    )
    monkeypatch.setattr(transcription.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        transcription.transcribe(tmp_path / "v.mp4", tmp_path / "t.json", cfg)


def test_unknown_backend_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="carrier-pigeon"):
        transcription.transcribe(
            tmp_path / "v.mp4", tmp_path / "t.json", make_settings("carrier-pigeon")
        )


def test_upload_retries_once_on_transport_error(tmp_path, monkeypatch):
    """A failover between hosts kills the in-flight request once; absorb it."""
    import httpx

    calls = {"n": 0}

    class FlakyClient:
        def __init__(self, *args, **kwargs): ...

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.TransportError("connection killed mid-upload")
            return httpx.Response(
                200, json={"segments": [{"start": 0.0, "text": "重试"}]}
            )

    monkeypatch.setattr(httpx, "Client", FlakyClient)
    monkeypatch.setattr(transcription, "_RETRY_DELAY", 0.0)
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"fake-audio")
    payload = transcription._upload(
        "https://relay.example/v1/transcribe", "m-key", audio
    )
    assert calls["n"] == 2
    assert payload["segments"][0]["text"] == "重试"


def test_upload_never_retries_http_answers(tmp_path, monkeypatch):
    """401/402/503 are real answers (auth/quota/vendor); retrying helps nobody."""
    import httpx

    calls = {"n": 0}

    class Always402:
        def __init__(self, *args, **kwargs): ...

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            calls["n"] += 1
            return httpx.Response(402, json={"detail": "quota exhausted"})

    monkeypatch.setattr(httpx, "Client", Always402)
    monkeypatch.setattr(transcription, "_RETRY_DELAY", 0.0)
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"fake-audio")
    with pytest.raises(RuntimeError, match="HTTP 402"):
        transcription._upload(
            "https://relay.example/v1/transcribe", "m-key", audio
        )
    assert calls["n"] == 1
