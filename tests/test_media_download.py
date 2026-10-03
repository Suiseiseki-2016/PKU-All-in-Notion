from __future__ import annotations

import httpx

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
        start, end = map(int, span.removeprefix("bytes=").split("-"))
        return httpx.Response(206, headers={"content-range": f"bytes {start}-{end}/{len(payload)}"},
                              content=payload[start:end])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = media._download_mp4(client, "https://media.example/video.mp4", target)
    assert result.path is None and "incomplete" in result.error
    assert target.read_bytes() == old
