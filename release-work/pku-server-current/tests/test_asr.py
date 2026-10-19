"""Unit tests for the OSS-backed Bailian ASR adapter, with no vendor calls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.asr import AsrError, BailianFileAsr, OssTempStore


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_object(self, key, source):
        self.objects[key] = b"".join(iter(lambda: source.read(3), b""))
        return SimpleNamespace(status=200)

    def sign_url(self, method, key, expires, slash_safe):
        assert method == "GET" and expires == 1800 and slash_safe
        return f"https://oss.example/{key}?signature=short-lived"

    def delete_object(self, key):
        self.deleted.append(key)


class FakeTranscription:
    @staticmethod
    def async_call(**kwargs):
        assert kwargs["model"] == "qwen-audio-3.0-asr-flash-filetrans"
        assert kwargs["file_urls"][0].startswith("https://oss.example/pku-temp/")
        return SimpleNamespace(status_code=200, message="", output={"task_id": "task"})

    @staticmethod
    def wait(submitted, **kwargs):
        assert submitted.output["task_id"] == "task"
        assert kwargs["wait_timeout"] == 1800
        return SimpleNamespace(
            status_code=200,
            message="",
            output={"results": [{"transcription_url": "https://vendor.example/result"}]},
        )


def test_bailian_asr_streams_hashes_and_deletes_audio():
    bucket = FakeBucket()
    asr = BailianFileAsr(
        OssTempStore(bucket),
        "not-a-real-key",
        transcription_api=FakeTranscription,
        download_json=lambda _: {
            "properties": {"language": "zh"},
            "transcripts": [
                {
                    "sentences": [
                        {"begin_time": 0, "end_time": 1200, "text": "第一句"},
                        {"begin_time": 1500, "end_time": 2200, "text": "第二句"},
                    ]
                }
            ],
        },
    )

    from io import BytesIO

    payload = asr(BytesIO(b"audio-bytes"), "lecture.m4a")
    assert payload == {
        "language": "zh",
        "duration_seconds": 3,
        "segments": [
            {"start": 0.0, "end": 1.2, "text": "第一句"},
            {"start": 1.5, "end": 2.2, "text": "第二句"},
        ],
    }
    key = next(iter(bucket.objects))
    assert key.startswith("pku-temp/")
    assert bucket.objects[key] == b"audio-bytes"
    assert bucket.deleted == [key]


def test_bailian_asr_deletes_audio_when_vendor_fails():
    bucket = FakeBucket()

    class FailingTranscription:
        @staticmethod
        def async_call(**kwargs):
            return SimpleNamespace(status_code=400, message="bad request")

    from io import BytesIO

    asr = BailianFileAsr(OssTempStore(bucket), "not-a-real-key", FailingTranscription)
    with pytest.raises(AsrError, match="submission failed"):
        asr(BytesIO(b"audio"), "lecture.m4a")
    assert bucket.deleted == [next(iter(bucket.objects))]


@pytest.mark.parametrize("transcripts", [[], [{"channel_id": 0, "text": "", "sentences": []}]])
def test_silent_audio_is_a_valid_empty_transcript(transcripts):
    assert BailianFileAsr._to_payload({"properties": {"language": "zh"}, "transcripts": transcripts}) == {
        "language": "zh", "duration_seconds": 0, "segments": [],
    }


@pytest.mark.parametrize("result", [{}, None, [], {"transcripts": None}, {"transcripts": {}}])
def test_missing_or_malformed_transcripts_remain_errors(result):
    with pytest.raises(AsrError, match="^DashScope returned no transcript sentences$"):
        BailianFileAsr._to_payload(result)


@pytest.mark.parametrize("failures", [1, 2, 3])
def test_completed_asr_retries_result_download_only(monkeypatch, failures):
    from urllib.error import URLError

    calls, sleeps = [], []
    monkeypatch.setattr("server.asr.time.sleep", sleeps.append)

    def download(url):
        calls.append(url)
        if len(calls) <= failures:
            raise URLError("connection reset")
        return {"transcripts": []}

    asr = BailianFileAsr(OssTempStore(FakeBucket()), "key", FakeTranscription, download)
    if failures == 3:
        with pytest.raises(URLError):
            asr.transcribe_uploaded("pku-temp/audio")
    else:
        assert asr.transcribe_uploaded("pku-temp/audio")["segments"] == []
    assert len(calls) == min(failures + 1, 3)
    assert sleeps == [1.0] * min(failures, 2)
