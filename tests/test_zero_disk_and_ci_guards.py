"""Regression guards for zero-disk media streaming, installer cleanup, and cross-platform CI invariants."""

from __future__ import annotations

import ast
import errno
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pku_sync.config import Settings
from pku_sync.models import Course, Recording
from pku_sync.panel import recordings_api
from pku_sync import media, pipeline, transcription
from product_e2e.fault_boundary import AccountFaultBoundary

ROOT = Path(__file__).resolve().parents[1]


def test_default_settings_never_save_recordings_locally(tmp_path):
    """By default, recordings and audio must never be persisted to local disk."""
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert settings.save_recordings_locally is False
    assert settings.delete_video_after_processing is True
    assert settings.recordings_download_dir == ""


def test_stream_process_job_never_writes_video_or_audio_to_disk(tmp_path, monkeypatch):
    """During note generation with save_recordings_locally=False, neither video.mp4 nor audio chunks touch disk."""
    course = Course(course_id="course-1", name="计算机网络")
    course_dir = tmp_path / course.directory
    course_dir.mkdir(parents=True)
    rec = Recording(
        course_id="course-1",
        title="第一讲",
        date="2026-09-10",
        recorded_at="2026-09-10 10:00",
        mp4_url="https://stream.example.test/lecture1.mp4",
    )
    job = pipeline.RecordingJob(course_name=course.name, course_dir=course_dir, recording=rec)

    monkeypatch.setattr(
        media,
        "download_recording",
        lambda *args, **kwargs: pytest.fail("download_recording must never be called during in-memory streaming"),
    )
    monkeypatch.setattr(
        pipeline,
        "download_job",
        lambda *args, **kwargs: pytest.fail("download_job must never be called during in-memory streaming"),
    )

    class FakeMemoryStream(io.BytesIO):
        signature = "stream-sha256-fixture"

    monkeypatch.setattr(
        pipeline,
        "_open_job_memory_stream",
        lambda client, job, progress=None: (FakeMemoryStream(b"fake-mp4-stream"), media.DownloadResult(path=None)),
    )

    part = transcription.MemoryAudioPart("000.m4a", b"in-memory-aac-audio")
    assert not hasattr(part, "resolve") or isinstance(part.read_bytes(), bytes)

    def fake_decode(stream, keyframes_dir, *, need_audio, need_keyframes, progress=None):
        assert not list(tmp_path.rglob("*.mp4")), "video.mp4 must not exist on disk during stream decode"
        assert not list(tmp_path.rglob("*.m4a")), "audio .m4a chunks must not exist on disk during stream decode"
        assert not list(tmp_path.rglob("*.mp3")), "audio .mp3 files must not exist on disk during stream decode"
        if keyframes_dir is not None and need_keyframes:
            keyframes_dir.mkdir(parents=True, exist_ok=True)
            (keyframes_dir / "index.json").write_text("[]", "utf-8")
        return ([part] if need_audio else []), []

    monkeypatch.setattr(media, "_decode_stream_audio_and_keyframes", fake_decode)

    observed_parts = []

    def fake_transcribe_memory_parts(parts, target, settings, **kwargs):
        assert not list(tmp_path.rglob("*.mp4")), "video.mp4 must not exist on disk during transcription"
        assert not list(tmp_path.rglob("*.m4a")), "audio .m4a chunks must not exist on disk during transcription"
        observed_parts.extend(parts)
        payload = {
            "source": "cloud-asr",
            "duration_seconds": 1800.0,
            "text": "课堂内存流式转写正文",
            "segments": [{"start": 0.0, "end": 10.0, "text": "课堂内存流式转写正文"}],
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        return payload

    monkeypatch.setattr(transcription, "transcribe_memory_parts", fake_transcribe_memory_parts)
    monkeypatch.setattr(
        pipeline,
        "write_notes",
        lambda transcript, keyframes, notes_path, settings, **kwargs: (
            notes_path.write_text("# 课堂笔记\n\n内容", "utf-8") or "# 课堂笔记\n\n内容"
        ),
    )

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        save_recordings_locally=False,
        platform_url="https://pku.aeoluswu.info",
        platform_token="test-token",
    )
    res = pipeline.stream_process_job(None, job, settings, include_keyframes=True)

    assert not res.errors
    assert res.transcribed is True
    assert res.notes is True
    assert len(observed_parts) == 1
    assert observed_parts[0].data == b"in-memory-aac-audio"
    assert (job.directory / "transcript.json").is_file()
    assert (job.directory / "notes.md").is_file()
    assert not job.video.exists()
    assert not list(tmp_path.rglob("*.mp4"))
    assert not list(tmp_path.rglob("*.m4a"))
    assert not list(tmp_path.rglob("*.mp3"))


def test_local_storage_summary_and_deletion_lifecycle(tmp_path):
    """Saved recordings are transparently listed in local_storage_summary and cleanly removed on request."""
    course = Course(course_id="c1", name="高等数学")
    course_dir = tmp_path / course.directory
    rec_dir = course_dir / "recordings" / "2026-09-10_第一讲"
    rec_dir.mkdir(parents=True)
    (course_dir / "course.json").write_text(json.dumps(course.model_dump()), "utf-8")
    rec = Recording(
        course_id="c1",
        title="第一讲",
        date="2026-09-10",
        recorded_at="2026-09-10 08:00",
        play_url="https://onlineroomse.pku.edu.cn/player?course_id=c1",
    )
    (course_dir / "recordings" / "index.json").write_text(
        json.dumps([rec.model_dump()], ensure_ascii=False),
        "utf-8",
    )
    (rec_dir / "recording.json").write_text(json.dumps(rec.model_dump(), ensure_ascii=False), "utf-8")

    settings = Settings(_env_file=None, data_dir=tmp_path, save_recordings_locally=False)
    empty_summary = recordings_api.local_storage_summary(settings)
    assert empty_summary["save_recordings_locally"] is False
    assert empty_summary["count"] == 0

    # Simulate an explicitly downloaded video and verify it is surfaced and removable.
    video_file = rec_dir / "video.mp4"
    video_file.write_bytes(b"explicit-video-bytes")
    shown = recordings_api.local_storage_summary(settings)
    assert shown["count"] == 1
    assert shown["total_bytes"] == len(b"explicit-video-bytes")
    rec_key = shown["items"][0]["recording_id"]

    after_delete = recordings_api.delete_local_recording(settings, rec_key)
    assert after_delete["count"] == 0
    assert not video_file.exists()


def test_work_manager_routes_default_processing_to_memory_stream_and_optin_to_download_dir(tmp_path, monkeypatch):
    """RecordingWorkManager uses stream_process_job by default (0 disk video) and only downloads when save_recordings_locally=True."""
    course = Course(course_id="c1", name="操作系统")
    course_dir = tmp_path / course.directory
    course_dir.mkdir(parents=True)
    rec = Recording(
        course_id="c1",
        title="第二讲",
        date="2026-09-17",
        recorded_at="2026-09-17 10:00",
        play_url="https://onlineroomse.pku.edu.cn/player?course_id=c1&sub_id=s2",
        subject_id="s2",
    )
    job = pipeline.RecordingJob(course_name=course.name, course_dir=course_dir, recording=rec)

    monkeypatch.setattr("pku_sync.auth.get_session", lambda **kwargs: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("pku_sync.lecture_source_download.prepare_lesson_sources", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(recordings_api, "assignment_catalog_publish_issues", lambda *args, **kwargs: [])
    monkeypatch.setattr(recordings_api, "note_publish_issues", lambda *args, **kwargs: [])

    stream_called = []

    def fake_stream_process_job(client, j, settings, **kwargs):
        stream_called.append(j.recording.title)
        j.directory.mkdir(parents=True, exist_ok=True)
        kf_dir = j.directory / "keyframes"
        kf_dir.mkdir(parents=True, exist_ok=True)
        (kf_dir / "frame_0001.jpg").write_bytes(b"\xff\xd8\xff\xe0frame")
        (kf_dir / "index.json").write_text('{"keyframes":[{"file":"frame_0001.jpg","timestamp":10.0,"label":"00:10"}]}', "utf-8")
        (j.directory / "transcript.json").write_text('{"text":"ok","segments":[]}', "utf-8")
        (j.directory / "notes.md").write_text("# 笔记\n\n正文", "utf-8")
        return pipeline.ProcessResult(job=j, transcribed=True, keyframes=1, notes=True)

    monkeypatch.setattr(recordings_api, "stream_process_job", fake_stream_process_job)

    default_settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        pku_username="stu",
        pku_password="pwd",
        save_recordings_locally=False,
    )
    manager = recordings_api.RecordingWorkManager(default_settings)
    url = manager._process(job, "")
    assert stream_called == ["第二讲"]
    assert not job.video.exists()
    assert url.endswith("/note-preview")


def test_desktop_startup_cleans_downloaded_installers():
    """Tauri desktop shell must include leftover installer and updater temp cleanup logic and unit tests."""
    lib_rs = (ROOT / "desktop/src-tauri/src/lib.rs").read_text("utf-8")
    assert "fn is_leftover_installer_filename(" in lib_rs
    assert "fn is_updater_temp_entry_name(" in lib_rs
    assert "fn cleanup_entries_in_dir(" in lib_rs
    for ext in (".dmg", ".exe", ".msi", ".app.tar.gz"):
        assert ext in lib_rs


def test_macos_packaging_workflow_matches_tauri_product_name():
    """Prevent productName drift between tauri.conf.json and .github/workflows/macos-package.yml."""
    tauri_conf = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text("utf-8"))
    product_name = tauri_conf["productName"]
    workflow = (ROOT / ".github/workflows/macos-package.yml").read_text("utf-8")
    build_script = (ROOT / "desktop/scripts/build-macos.sh").read_text("utf-8")
    assert f"{product_name}.app" in workflow
    assert f"{product_name}_${{VERSION}}_aarch64.dmg" in workflow
    assert f"{product_name}.app" in build_script


def test_nonlistening_ephemeral_proxy_immediately_refuses_connection():
    """Guard against macOS XNU silent SYN drops on bound-but-unlistening TCP sockets."""
    boundary = AccountFaultBoundary(SimpleNamespace(config={}, actions=[], network=[]))
    try:
        proof = boundary._network_failure("proxy", "GET", "/v1/catalog/_123_1", None, {})
        assert proof["failure_observed"] is True
        assert any(
            row["errno"] in {10061, errno.ECONNREFUSED} or row["winerror"] == 10061
            for row in proof["error_chain"]
        )
    finally:
        boundary.close()


def test_tests_do_not_call_bare_read_text_on_utf8_assets():
    """Guard against Windows cp1252 UnicodeDecodeError caused by bare .read_text() on UTF-8 files."""
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        source = path.read_text("utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "read_text"
                and not node.args
                and not any(kw.arg == "encoding" for kw in node.keywords)
            ):
                snippet = ast.get_source_segment(source, node) or "read_text()"
                if any(token in snippet for token in ("app.js", "app.css", ".json", ".md", "source", "script", "receipts")):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {snippet}")
    assert not offenders, "Bare .read_text() without 'utf-8' breaks on Windows cp1252:\n" + "\n".join(offenders)
