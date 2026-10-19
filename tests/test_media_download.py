from __future__ import annotations

import httpx
import pytest

from pku_sync import media


def test_mp4_probe_only_reads_headers_when_server_ignores_range():
    calls = []
    class NoRead(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("size probe must not download the movie")
        def close(self):
            pass
    def handler(request):
        calls.append(request.headers.get("range"))
        return httpx.Response(200, headers={"content-length": "500000000"}, stream=NoRead())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert media._probe_mp4(client, "https://media.example/video.mp4", {}) == (500000000, False)
    assert calls == ["bytes=0-0"]


def test_mp4_parallel_ranges_reassemble_and_report_progress(tmp_path, monkeypatch):
    payload = bytes(range(256)) * 4096
    monkeypatch.setattr(media, "MP4_PARALLEL_MIN", 1)
    ranges = []
    def handler(request):
        span = request.headers.get("range")
        assert span
        start, end = map(int, span.removeprefix("bytes=").split("-"))
        ranges.append((start, end))
        return httpx.Response(206, headers={"content-range": f"bytes {start}-{end}/{len(payload)}"},
                              content=payload[start:end+1])
    seen = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = media._download_mp4(client, "https://media.example/video.mp4", tmp_path / "video.mp4",
                                     progress=lambda *args: seen.append(args))
    assert result.path and result.path.read_bytes() == payload
    assert len(ranges) == media.MP4_WORKERS + 1  # one header probe
    assert seen[-1] == (len(payload), len(payload), "bytes", len(payload))


def test_mp4_server_without_ranges_streams_once_and_reports_progress(tmp_path):
    payload = b"video" * 1000
    requests = []
    def handler(request):
        requests.append(request.headers.get("range"))
        return httpx.Response(200, content=payload)
    seen = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = media._download_mp4(client, "https://media.example/video.mp4", tmp_path / "video.mp4",
                                     progress=lambda *args: seen.append(args))
    assert result.path and result.path.read_bytes() == payload
    assert requests == ["bytes=0-0", None]
    assert seen[-1][0] == len(payload)


def test_mp4_advertised_ranges_but_ignored_falls_back_without_corrupting_file(tmp_path, monkeypatch):
    payload = bytes(range(64)) * 128
    monkeypatch.setattr(media, "MP4_PARALLEL_MIN", 1)
    requests = []
    def handler(request):
        span = request.headers.get("range")
        requests.append(span)
        if span == "bytes=0-0":
            return httpx.Response(206, headers={"content-range": f"bytes 0-0/{len(payload)}"}, content=payload[:1])
        return httpx.Response(200, content=payload)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = media._download_mp4(client, "https://media.example/video.mp4", tmp_path / "video.mp4")
    assert result.path and result.path.read_bytes() == payload
    assert requests[-1] is None


def test_incomplete_parallel_range_keeps_existing_video_untouched(tmp_path, monkeypatch):
    payload = b"new recording" * 1024
    target = tmp_path / "video.mp4"
    old = b"old" * len(payload)
    target.write_bytes(old)
    monkeypatch.setattr(media, "MP4_PARALLEL_MIN", 1)
    def handler(request):
        span = request.headers.get("range")
        if span == "bytes=0-0":
            return httpx.Response(206, headers={"content-range": f"bytes 0-0/{len(payload)}"}, content=payload[:1])
        if span is None:
            return httpx.Response(200, content=payload[:-1])
        start_text, end_text = span.removeprefix("bytes=").split("-")
        start, end = int(start_text), int(end_text) if end_text else len(payload) - 1
        return httpx.Response(206, headers={"content-range": f"bytes {start}-{end}/{len(payload)}"},
                              content=payload[start:end])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = media._download_mp4(client, "https://media.example/video.mp4", target)
    assert result.path is None and "incomplete" in result.error
    assert target.read_bytes() == old


class DroppedStream(httpx.SyncByteStream):
    def __init__(self, prefix):
        self.prefix = prefix

    def __iter__(self):
        yield self.prefix
        raise httpx.ReadError("connection dropped")


def test_range_part_resumes_after_midstream_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CHUNK", 2)
    payload = b"abcdefghijklmnop"
    requests = []
    def handle(request):
        span = request.headers["range"]
        requests.append(span)
        start, end = map(int, span.removeprefix("bytes=").split("-"))
        headers = {"content-range": f"bytes {start}-{end}/{len(payload)}"}
        if span == "bytes=0-3":
            return httpx.Response(206, headers=headers, stream=DroppedStream(payload[:2]))
        return httpx.Response(206, headers=headers, content=payload[start:end + 1])
    target = tmp_path / "video.mp4"
    progress = []
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = media._download_mp4_ranges(client, "https://media.example/video", target, {}, len(payload),
            lambda *args: progress.append(args))
    assert result.path == target and target.read_bytes() == payload
    assert requests.count("bytes=0-3") == requests.count("bytes=2-3") == 1
    assert progress[-1] == (16, 16, "bytes", 16)
    assert not (tmp_path / ".video.mp4-parts-16").exists()


def test_failed_ranges_persist_and_completed_parts_are_reused(tmp_path):
    payload = b"abcdefghijklmnop"
    requests = []
    failing = True
    def handle(request):
        span = request.headers["range"]
        requests.append(span)
        start, end = map(int, span.removeprefix("bytes=").split("-"))
        if start == 0 and failing:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(206, headers={"content-range": f"bytes {start}-{end}/16"},
            content=payload[start:end + 1])
    target = tmp_path / "video.mp4"
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = media._download_mp4_ranges(client, "https://media.example/video", target, {}, 16, None)
        assert result.path is None
        parts = tmp_path / ".video.mp4-parts-16"
        assert len(list(parts.glob("*.part"))) == 3
        assert requests.count("bytes=0-3") == 3
        previous = len(requests)
        failing = False
        result = media._download_mp4_ranges(client, "https://media.example/video", target, {}, 16, None)
    assert requests[previous:] == ["bytes=0-3"]
    assert result.path == target and target.read_bytes() == payload
    assert not parts.exists()


def test_range_network_failure_falls_back_to_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MP4_PARALLEL_MIN", 1)
    payload = b"abcdefghijklmnop"
    requests = []
    def handle(request):
        span = request.headers.get("range")
        requests.append(span)
        if span == "bytes=0-0":
            return httpx.Response(206, headers={"content-range": "bytes 0-0/16"}, content=payload[:1])
        if span is not None:
            raise httpx.ReadError("range disconnected", request=request)
        return httpx.Response(200, content=payload)
    target = tmp_path / "video.mp4"
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = media._download_mp4(client, "https://media.example/video", target)
    assert result.path == target and target.read_bytes() == payload
    assert requests[-1] is None
    assert not (tmp_path / ".video.mp4-parts-16").exists()


@pytest.mark.parametrize("ignore_range", [False, True])
def test_single_stream_resumes_after_disconnect(tmp_path, monkeypatch, ignore_range):
    monkeypatch.setattr(media, "CHUNK", 2)
    payload = b"abcdefgh"
    requests = []
    def handle(request):
        span = request.headers.get("range")
        requests.append(span)
        if len(requests) == 1:
            return httpx.Response(200, headers={"content-length": "8"})
        if len(requests) == 2:
            return httpx.Response(200, stream=DroppedStream(payload[:4]))
        if ignore_range:
            return httpx.Response(200, content=payload)
        assert span == "bytes=4-"
        return httpx.Response(206, headers={"content-range": "bytes 4-7/8"}, content=payload[4:])
    target = tmp_path / "video.mp4"
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = media._download_mp4(client, "https://media.example/video", target)
    assert result.path == target and target.read_bytes() == payload
    assert requests == ["bytes=0-0", None, "bytes=4-"]
    assert result.resumed == (not ignore_range)
    assert not target.with_name("video.mp4.part").exists()


def test_single_stream_keeps_partial_bytes_after_three_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CHUNK", 2)
    requests = []
    def handle(request):
        span = request.headers.get("range")
        requests.append(span)
        if len(requests) == 1:
            return httpx.Response(200, headers={"content-length": "12"})
        if span is None:
            return httpx.Response(200, stream=DroppedStream(b"ab"))
        start = int(span.removeprefix("bytes=").rstrip("-"))
        return httpx.Response(206, headers={"content-range": f"bytes {start}-11/12"},
            stream=DroppedStream(b"cd"))
    target = tmp_path / "video.mp4"
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = media._download_mp4(client, "https://media.example/video", target)
    assert result.path is None and "connection dropped" in result.error
    assert requests == ["bytes=0-0", None, "bytes=2-", "bytes=4-"]
    assert not target.exists()
    assert (tmp_path / "video.mp4.part").read_bytes() == b"abcdcd"


def test_next_download_resumes_saved_stream_instead_of_restarting_ranges(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MP4_PARALLEL_MIN", 1)
    payload = b"abcdefgh"
    (tmp_path / "video.mp4.part").write_bytes(payload[:4])
    requests = []
    def handle(request):
        span = request.headers["range"]
        requests.append(span)
        if span == "bytes=0-0":
            return httpx.Response(206, headers={"content-range": "bytes 0-0/8"}, content=payload[:1])
        assert span == "bytes=4-"
        return httpx.Response(206, headers={"content-range": "bytes 4-7/8"}, content=payload[4:])
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = media._download_mp4(client, "https://media.example/video", tmp_path / "video.mp4")
    assert result.path.read_bytes() == payload and result.resumed
    assert requests == ["bytes=0-0", "bytes=4-"]


def test_range_resume_can_restart_when_server_returns_original_full_part(tmp_path):
    payload = b"abcdefghijklmnop"
    parts = tmp_path / ".video.mp4-parts-16"
    parts.mkdir()
    (parts / "000.part").write_bytes(b"ab")
    requests = []
    def handle(request):
        span = request.headers["range"]
        requests.append(span)
        start, end = map(int, span.removeprefix("bytes=").split("-"))
        if span == "bytes=2-3":
            start = 0
        return httpx.Response(206, headers={"content-range": f"bytes {start}-{end}/16"},
            content=payload[start:end + 1])
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = media._download_mp4_ranges(client, "https://media.example/video", tmp_path / "video.mp4", {}, 16, None)
    assert result.path.read_bytes() == payload
    assert "bytes=2-3" in requests and "bytes=0-3" not in requests


def test_cancelled_range_download_does_not_start_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MP4_PARALLEL_MIN", 1)
    monkeypatch.setattr(media, "_probe_mp4", lambda *args: (16, True))
    monkeypatch.setattr(media, "_download_mp4_ranges", lambda *args:
        media.DownloadResult(path=None, error="cancelled"))
    def handle(request):
        pytest.fail("cancelled download must not open a single stream")
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = media._download_mp4(client, "https://media.example/video", tmp_path / "video.mp4")
    assert result.error == "cancelled"
