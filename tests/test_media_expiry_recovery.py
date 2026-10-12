"""Developer regressions; installed HTTP-boundary evidence is separate."""
import json

import httpx
import pytest

from pku_sync import media, pipeline
from pku_sync.models import Recording


def job_at(root):
    job = pipeline.RecordingJob('课程', root / 'course', Recording(
        course_id='_123_1', title='第1讲', recorded_at='2026-09-24 09:00:00',
        play_url='player', mp4_url='https://media.example/stale'))
    job.directory.mkdir(parents=True)
    (job.directory / 'answers.md').write_text('学生原答案', 'utf8')
    (job.directory / 'recording.json').write_text(
        json.dumps(job.recording.model_dump(), ensure_ascii=False), 'utf8')
    return job


@pytest.mark.parametrize('status', [401, 403, 410])
def test_expired_media_renews_metadata_once_and_keeps_student_content(tmp_path, monkeypatch, status):
    job = job_at(tmp_path)
    resolved, urls = [], []
    payload = b'full original recording'
    def resolve(client, recording):
        resolved.append(True)
        recording.mp4_url = f'https://media.example/generation-{len(resolved)}'
    def handle(request):
        urls.append(str(request.url))
        if request.url.path == '/generation-1':
            return httpx.Response(status)
        return httpx.Response(200, content=payload)
    monkeypatch.setattr(pipeline, 'resolve_media', resolve)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = pipeline.download_job(client, job)
    assert result.path == job.video and job.video.read_bytes() == payload
    assert len(resolved) == 2 and urls.count('https://media.example/generation-1') == 1
    assert (job.directory / 'answers.md').read_text('utf8') == '学生原答案'
    assert json.loads((job.directory / 'recording.json').read_text('utf8'))['mp4_url'].endswith('generation-2')


def test_persistent_expiry_stops_after_one_renewal_and_preserves_old_files(tmp_path, monkeypatch):
    job = job_at(tmp_path)
    job.video.write_bytes(b'existing partial video')
    old_metadata = (job.directory / 'recording.json').read_bytes()
    calls, resolved = [], []
    def resolve(client, recording):
        resolved.append(True)
        recording.mp4_url = 'https://media.example/new-address'
    monkeypatch.setattr(pipeline, 'resolve_media', resolve)
    with httpx.Client(transport=httpx.MockTransport(lambda req:
            calls.append(req) or httpx.Response(403))) as client:
        result = pipeline.download_job(client, job)
    assert result.path is None and getattr(result, 'http_status', None) == 403
    assert len(resolved) == len(calls) == 2
    assert job.video.read_bytes() == b'existing partial video'
    assert (job.directory / 'recording.json').read_bytes() == old_metadata


def test_renewal_without_media_does_not_replay_stale_address(tmp_path, monkeypatch):
    job = job_at(tmp_path)
    resolved, calls = [], []
    def resolve(client, recording):
        resolved.append(True)
        if len(resolved) == 1:
            recording.mp4_url = 'https://media.example/expired'
        else:
            recording.unavailable_reason = 'not published'
    monkeypatch.setattr(pipeline, 'resolve_media', resolve)
    with httpx.Client(transport=httpx.MockTransport(lambda req:
            calls.append(req) or httpx.Response(403))) as client:
        result = pipeline.download_job(client, job)
    assert result.path is None and result.error == 'not published'
    assert len(resolved) == 2 and len(calls) == 1
    assert not job.video.exists()


@pytest.mark.parametrize('status', [429, 500, 503])
def test_other_http_failures_do_not_renew_metadata(tmp_path, monkeypatch, status):
    job = job_at(tmp_path)
    resolved = []
    monkeypatch.setattr(pipeline, 'resolve_media', lambda *args: resolved.append(True))
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status))) as client:
        result = pipeline.download_job(client, job)
    assert result.path is None and len(resolved) == 1
    assert not job.video.exists()


def test_network_failure_does_not_masquerade_as_expiry(tmp_path, monkeypatch):
    job = job_at(tmp_path)
    resolved = []
    monkeypatch.setattr(pipeline, 'resolve_media', lambda *args: resolved.append(True))
    def fail(req):
        raise httpx.ConnectError('HTTP 403 in diagnostic text', request=req)
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        result = pipeline.download_job(client, job)
    assert result.path is None and len(resolved) == 1
    assert getattr(result, 'http_status', None) is None


def test_parallel_expiry_is_not_hidden_as_missing_range_support(tmp_path, monkeypatch):
    monkeypatch.setattr(media, 'MP4_PARALLEL_MIN', 1)
    calls = []
    def handle(req):
        calls.append(req.headers.get('range'))
        if req.headers.get('range') == 'bytes=0-0':
            return httpx.Response(206, headers={'content-range': 'bytes 0-0/12000'}, content=b'x')
        return httpx.Response(403)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = media._download_mp4(client, 'https://media.example/expired', tmp_path / 'video.mp4')
    assert result.path is None and getattr(result, 'http_status', None) == 403
    assert None not in calls and not (tmp_path / 'video.mp4').exists()


@pytest.mark.parametrize('stage', ['playlist', 'segment', 'key'])
def test_hls_expiry_reaches_renewal_without_ffmpeg_or_stale_mp4_fallback(tmp_path, monkeypatch, stage):
    recording = Recording(course_id='_123_1', title='第1讲', recorded_at='2026-09-24 09:00:00',
        m3u8_url='https://media.example/playlist', mp4_url='https://media.example/stale.mp4')
    playlist = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:10,\nsegment\n'
    def handle(req):
        if req.url.path == '/' + stage:
            return httpx.Response(403)
        if req.url.path == '/playlist':
            return httpx.Response(200, text=playlist)
        if req.url.path == '/segment':
            return httpx.Response(200, content=b'x' * 16)
        raise AssertionError('must renew the selected source before another fallback')
    monkeypatch.setattr(media, '_download_hls', lambda *args: pytest.fail('stale ffmpeg fallback'))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = media.download_recording(client, recording, tmp_path)
    assert result.path is None and getattr(result, 'http_status', None) == 403
    assert not (tmp_path / 'video.mp4').exists()


def test_renewed_hls_plan_does_not_merge_prior_cached_segments(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    job = job_at(tmp_path)
    resolved = []
    def resolve(client, recording):
        resolved.append(True)
        recording.mp4_url = ''
        recording.m3u8_url = f'https://media.example/v{len(resolved)}/playlist'
    def handle(req):
        if req.url.path.endswith('/playlist'):
            return httpx.Response(200, text='#EXTM3U\n#EXTINF:4,\nseg0\n#EXTINF:4,\nseg1\n')
        if req.url.path == '/v1/seg1':
            return httpx.Response(403)
        return httpx.Response(200, content={
            '/v1/seg0': b'old original segment', '/v2/seg0': b'new original segment',
            '/v2/seg1': b'new final segment'}[req.url.path])
    def remux(argv, **kwargs):
        Path(argv[-1]).write_bytes(Path(argv[argv.index('-i') + 1]).read_bytes())
        return SimpleNamespace(returncode=0, stderr='')
    monkeypatch.setattr(pipeline, 'resolve_media', resolve)
    monkeypatch.setattr(media.shutil, 'which', lambda name: 'ffmpeg')
    monkeypatch.setattr(media.subprocess, 'run', remux)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = pipeline.download_job(client, job)
    assert len(resolved) == 2 and result.path == job.video
    assert job.video.read_bytes() == b'new original segmentnew final segment'
    old_parts = list((job.directory / '.video.mp4.hls').glob('*/seg_00000.ts'))
    assert len(old_parts) == 1 and old_parts[0].read_bytes() == b'old original segment'
    assert (job.directory / 'answers.md').read_text('utf8') == '学生原答案'
