"""In-process audio extraction for the cloud transcription backend.

VAL-PKG-010: a packaged install must extract the recording's audio track
inside its own process — no system ffmpeg binary, no child process — and every
extraction failure must reach the student as actionable copy instead of a
stack trace.

The fixture clip is generated with PyAV rather than committed as a binary so
the fixture stays reviewable in text form and reproducible on every platform
of the CI matrix.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from pku_sync import transcription

SOURCE_RATE = 48000
CLIP_SECONDS = 1.5
VIDEO_FPS = 10


def _audio_frames(rate: int, seconds: float):
    """Planar-stereo 440 Hz frames covering ``seconds`` of audio."""
    import av
    import numpy as np

    total = int(rate * seconds)
    position = 0
    while position < total:
        count = min(1024, total - position)
        moment = np.arange(position, position + count) / float(rate)
        wave = (0.3 * np.sin(2 * math.pi * 440 * moment)).astype("float32")
        frame = av.AudioFrame.from_ndarray(
            np.vstack([wave, wave]), format="fltp", layout="stereo"
        )
        frame.sample_rate = rate
        frame.pts = position
        frame.time_base = Fraction(1, rate)
        yield frame
        position += count


def make_fixture_clip(
    target: Path,
    seconds: float = CLIP_SECONDS,
    rate: int = SOURCE_RATE,
    with_audio: bool = True,
) -> Path:
    """A real mp4 shaped like a downloaded lecture: video plus stereo AAC."""
    import av
    import numpy as np

    target.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(target), mode="w") as container:
        video = container.add_stream("mpeg4", rate=VIDEO_FPS)
        video.width, video.height = 160, 120
        video.pix_fmt = "yuv420p"
        audio = container.add_stream("aac", rate=rate) if with_audio else None

        if audio is not None:
            for frame in _audio_frames(rate, seconds):
                for packet in audio.encode(frame):
                    container.mux(packet)
            for packet in audio.encode(None):
                container.mux(packet)

        for index in range(int(VIDEO_FPS * seconds)):
            image = np.full((120, 160, 3), (index * 17) % 256, dtype="uint8")
            frame = av.VideoFrame.from_ndarray(image, format="rgb24")
            frame.pts = index
            for packet in video.encode(frame):
                container.mux(packet)
        for packet in video.encode(None):
            container.mux(packet)
    return target


def describe_audio(path: Path) -> dict:
    """Decoded shape of an extracted track, read back through PyAV."""
    import av

    with av.open(str(path)) as container:
        streams = container.streams
        audio = next(stream for stream in streams if stream.type == "audio")
        samples = sum(
            frame.samples for frame in container.decode(audio)
        )
        return {
            "rate": audio.rate,
            "channels": audio.channels,
            "layout": audio.layout.name,
            "video_streams": len([s for s in streams if s.type == "video"]),
            "samples": samples,
        }


def cloud_settings(**extra) -> SimpleNamespace:
    values = {
        "transcription_backend": "cloud",
        "cloud_transcribe_url": "https://relay.example/v1/transcribe",
        "platform_token": "scratch-session",
    }
    values.update(extra)
    return SimpleNamespace(**values)


def test_extraction_writes_a_16k_mono_audio_only_track(tmp_path):
    clip = make_fixture_clip(tmp_path / "video.mp4")
    out = transcription._extract_audio(clip, tmp_path / "audio.m4a")

    assert out.exists() and out.stat().st_size > 0
    shape = describe_audio(out)
    assert shape["rate"] == 16000
    assert shape["channels"] == 1
    assert shape["layout"] == "mono"
    # SERVICE_PLAN §3.2: audio only ever leaves the machine, never the video.
    assert shape["video_streams"] == 0
    expected = 16000 * CLIP_SECONDS
    assert abs(shape["samples"] - expected) < 16000 * 0.25


def test_extraction_never_spawns_a_child_process(tmp_path, monkeypatch):
    clip = make_fixture_clip(tmp_path / "video.mp4")

    def forbidden(*args, **kwargs):
        raise AssertionError("cloud extraction must not spawn a child process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)

    out = transcription._extract_audio(clip, tmp_path / "audio.m4a")
    assert describe_audio(out)["rate"] == 16000
    # The module no longer reaches for a process-spawning API at all.
    assert not hasattr(transcription, "subprocess")


def test_extraction_succeeds_with_no_ffmpeg_anywhere_on_path(tmp_path, monkeypatch):
    clip = make_fixture_clip(tmp_path / "video.mp4")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(transcription.shutil, "which", lambda name: None)

    out = transcription._extract_audio(clip, tmp_path / "audio.m4a")
    assert describe_audio(out)["channels"] == 1


def test_missing_recording_reports_actionable_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(transcription.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError) as raised:
        transcription._extract_audio(tmp_path / "absent.mp4", tmp_path / "a.m4a")

    message = str(raised.value)
    assert "Traceback" not in message and "\n" not in message
    assert "无需安装 ffmpeg" in message
    assert "TRANSCRIPTION_BACKEND=local" in message
    # The old advice (install the binary) must never be shown again.
    assert "install ffmpeg" not in message and "请安装 ffmpeg" not in message


def test_recording_without_an_audio_track_reports_actionable_copy(tmp_path):
    clip = make_fixture_clip(tmp_path / "silent.mp4", with_audio=False)
    with pytest.raises(RuntimeError) as raised:
        transcription._extract_audio(clip, tmp_path / "audio.m4a")

    message = str(raised.value)
    assert "音轨" in message
    assert "Traceback" not in message and "\n" not in message
    assert "ffmpeg" in message


def test_degraded_install_without_the_bundled_decoder_is_actionable(
    tmp_path, monkeypatch
):
    clip = make_fixture_clip(tmp_path / "video.mp4")
    # A broken install: importing the bundled decoder fails at call time.
    monkeypatch.setitem(sys.modules, "av", None)

    with pytest.raises(RuntimeError) as raised:
        transcription._extract_audio(clip, tmp_path / "audio.m4a")

    message = str(raised.value)
    assert "重新安装" in message
    assert "Traceback" not in message and "\n" not in message
    assert "ffmpeg" in message
    assert not isinstance(raised.value, ImportError)


def test_decoder_failure_is_wrapped_with_support_detail(tmp_path, monkeypatch):
    import av

    clip = make_fixture_clip(tmp_path / "video.mp4")

    def explode(*args, **kwargs):
        raise ValueError("moov atom not found")

    monkeypatch.setattr(av, "open", explode)
    with pytest.raises(RuntimeError) as raised:
        transcription._extract_audio(clip, tmp_path / "audio.m4a")

    message = str(raised.value)
    assert "moov atom not found" in message
    assert "Traceback" not in message and "\n" not in message


def test_decoder_note_never_tells_the_student_to_install_ffmpeg(monkeypatch):
    monkeypatch.setattr(transcription.shutil, "which", lambda name: None)
    absent = transcription._decoder_note()
    monkeypatch.setattr(
        transcription.shutil, "which", lambda name: r"C:\tools\ffmpeg.exe"
    )
    present = transcription._decoder_note()

    for note in (absent, present):
        assert "ffmpeg" in note
        assert "install ffmpeg" not in note and "请安装 ffmpeg" not in note
    assert "无需安装 ffmpeg" in absent
    # When a binary happens to exist, say plainly that it is not used here.
    assert "不参与" in present


def test_cloud_backend_uploads_the_in_process_extracted_audio(tmp_path, monkeypatch):
    clip = make_fixture_clip(tmp_path / "video.mp4")
    monkeypatch.setenv("PATH", "")
    uploaded: dict = {}

    def fake_upload(url, key, audio):
        path = Path(audio)
        uploaded["path"] = path
        uploaded["shape"] = describe_audio(path)
        return {"language": "zh", "segments": [{"start": 0.0, "text": "第一讲"}]}

    monkeypatch.setattr(transcription, "_upload", fake_upload)
    target = tmp_path / "transcript.json"
    payload = transcription.transcribe(clip, target, cloud_settings())

    assert payload["segments"][0]["text"] == "第一讲"
    assert uploaded["path"].suffix == ".m4a"
    assert uploaded["path"] != clip
    assert uploaded["shape"] == {
        "rate": 16000,
        "channels": 1,
        "layout": "mono",
        "video_streams": 0,
        "samples": uploaded["shape"]["samples"],
    }
    assert json.loads(target.read_text("utf-8")) == payload
    # The temporary audio never outlives the upload.
    assert not uploaded["path"].exists()
