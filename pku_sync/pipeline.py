"""Recording pipeline: download, then transcribe, keyframe and take notes.

The two halves are separate on purpose. Downloading needs a live Blackboard
session; processing needs a GPU and no network. Splitting them lets the Mac fetch
and the Windows host transcribe from the same directory tree.

Work is discovered from the `recordings/index.json` files written by `sync`, so
neither half has to re-crawl the course site.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import httpx

from .media import (DownloadResult, EXPIRED_MEDIA_STATUSES, download_recording,
                    extract_keyframes, write_notes)
from .models import Recording
from .note_sources import collect_note_sources
from .recordings import list_recordings, resolve_media
from .store import safe_name
from .transcription import transcribe


@dataclass
class RecordingJob:
    course_name: str
    course_dir: Path
    recording: Recording

    @cached_property
    def directory(self) -> Path:
        root = self.course_dir / "recordings"
        proposed = root / safe_name(self.recording.slug, "recording")
        from .catalog_snapshots import recording_source_id
        try:
            identity = recording_source_id(self.recording)
        except ValueError:
            return proposed  # Legacy unidentifiable caches remain local-only.
        matches = []
        foreign_proposed = False
        for marker in root.glob("*/recording.json"):
            if not marker.resolve().is_relative_to(root.resolve()):
                continue
            try:
                if marker.stat().st_size > 256_000:
                    continue
                previous = Recording.model_validate_json(marker.read_text("utf-8"))
                same = (previous.course_id == self.recording.course_id
                        and recording_source_id(previous) == identity)
            except (OSError, ValueError):
                continue
            if same:
                matches.append(marker.parent)
            elif marker.parent == proposed:
                foreign_proposed = True
        if len(matches) > 1:
            raise ValueError("同一教学网讲次对应多个本地目录，请先核对；已有文件均已保留。")
        if matches:
            return matches[0]
        if foreign_proposed:
            # Same-day equal titles are not the same teaching-portal session.
            import hashlib
            suffix = hashlib.sha256((self.recording.course_id + "\n" + identity).encode()).hexdigest()[:24]
            return root / ("recording-" + suffix)
        return proposed

    @property
    def video(self) -> Path:
        return self.directory / "video.mp4"

    @property
    def label(self) -> str:
        return f"{self.course_name} · {self.recording.title}"


@dataclass
class ProcessResult:
    job: RecordingJob
    transcribed: bool = False
    keyframes: int = 0
    notes: bool = False
    removed_video: bool = False
    errors: list[str] = field(default_factory=list)


def collect_jobs(root: Path, course_filter: str = "") -> list[RecordingJob]:
    """Read every recording index written by a previous `sync` run."""
    jobs: list[RecordingJob] = []
    for index in sorted(root.glob("*/recordings/index.json")):
        course_dir = index.parent.parent
        course_name = _course_name(course_dir)
        try:
            entries = json.loads(index.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        from .catalog_snapshots import cached
        try:
            metadata = json.loads((course_dir / "course.json").read_text("utf-8"))
            canonical = cached(root, metadata.get("course_id", "")) if isinstance(metadata, dict) else None
        except (OSError, ValueError):
            canonical = None
        if not isinstance(entries, list):
            continue
        if canonical:
            snapshot = canonical["snapshot"]
            course_name = snapshot["course"]["name"]
            private = {entry.get("recorded_at"): entry for entry in entries if isinstance(entry, dict)}
            entries = [{**{key: value for key, value in private.get(row["recorded_at"], {}).items()
                           if key in {"play_url", "mp4_url", "m3u8_url", "platform_course_id", "subject_id", "room", "unavailable_reason"}},
                        **row, "course_id": snapshot["course_id"]}
                       for row in snapshot["sections"]["recordings"]["items"]]
        for entry in entries:
            try:
                recording = Recording.model_validate(entry)
            except ValueError:
                continue
            if course_filter and recording.course_id != course_filter:
                continue
            jobs.append(RecordingJob(course_name, course_dir, recording))
    return jobs


def _course_name(course_dir: Path) -> str:
    meta = course_dir / "course.json"
    if meta.exists():
        try:
            item = json.loads(meta.read_text("utf-8"))
            return (item.get("name") or course_dir.name) if isinstance(item, dict) else course_dir.name
        except (json.JSONDecodeError, OSError):
            pass
    return course_dir.name


def recording_stage(job: RecordingJob) -> tuple[str, str]:
    """One recording's pipeline stage as ``(status, unavailable_reason)``.

    Single source for the five-stage ladder — ``unavailable`` →
    ``notes_ready`` → ``transcribed`` → ``downloaded`` → ``indexed`` — shared
    by the MCP ``list_recording_status`` tool and the TUI's course panel, so
    the two can never disagree about what "processed" means. A
    ``recording.json`` on disk overrides the index's ``unavailable_reason``
    (media resolution can fail after the index was written).
    """
    directory = job.directory
    unavailable = job.recording.unavailable_reason
    record_path = directory / "recording.json"
    if record_path.exists():
        try:
            unavailable = (
                json.loads(record_path.read_text("utf-8")).get("unavailable_reason", "")
                or unavailable
            )
        except (OSError, json.JSONDecodeError):
            pass
    if unavailable:
        status = "unavailable"
    elif (directory / "notes.md").exists():
        status = "notes_ready"
    elif (directory / "transcript.json").exists():
        status = "transcribed"
    elif job.video.exists():
        status = "downloaded"
    else:
        status = "indexed"
    return status, unavailable


def _resolve_job_media(client: httpx.Client, job: RecordingJob) -> str | None:
    try:
        try:
            resolve_media(client, job.recording)
            needs_refresh = not job.recording.media_url
        except Exception:
            needs_refresh = True
        if needs_refresh:
            candidates = list_recordings(client, job.recording.course_id)
            refreshed = next((rec for rec in candidates if job.recording.subject_id
                              and rec.subject_id == job.recording.subject_id), None)
            if refreshed is None:
                # Blackboard's listing normally has no streaming subject ids.
                # Fall back to its title/date identity only for unlabelled rows;
                # an explicitly different subject must never be selected.
                refreshed = next((rec for rec in candidates
                                  if (not job.recording.subject_id or not rec.subject_id)
                                  and rec.title == job.recording.title
                                  and rec.date == job.recording.date), None)
            if refreshed is None:
                return "resolve failed: recording missing from refreshed catalog"
            job.recording.play_url = refreshed.play_url
            job.recording.mp4_url = ""
            job.recording.m3u8_url = ""
            job.recording.unavailable_reason = ""
            resolve_media(client, job.recording)
    except Exception as exc:
        return f"resolve failed: {type(exc).__name__}"
    return None


def download_job(client: httpx.Client, job: RecordingJob, progress=None) -> DownloadResult:
    """Download one recording, re-resolving its URL first.

    The media host signs playback URLs with a short lifetime, so a URL stored by
    an earlier `sync` is usually stale by the time a download starts.
    """
    resolve_error = _resolve_job_media(client, job)
    if resolve_error:
        return DownloadResult(path=None, error=resolve_error)

    result = download_recording(client, job.recording, job.directory, progress=progress)
    if result.path is None and result.http_status in EXPIRED_MEDIA_STATUSES:
        # A media URL may expire after its initial resolution. Renew the player
        # chain once, never classify network/disk failures from diagnostic text.
        # Empty or rejected fresh metadata must not replay the previous address.
        job.recording.mp4_url = ""
        job.recording.m3u8_url = ""
        job.recording.unavailable_reason = ""
        try:
            resolve_media(client, job.recording)
        except Exception as exc:
            return DownloadResult(path=None, error=f"resolve failed: {type(exc).__name__}")
        result = download_recording(client, job.recording, job.directory, progress=progress)
    if result.path is not None:
        (job.directory / "recording.json").write_text(
            json.dumps(job.recording.model_dump(), ensure_ascii=False, indent=1), "utf-8"
        )
    return result


def _open_job_memory_stream(client: httpx.Client, job: RecordingJob, progress=None):
    """Resolve URL and open an in-memory stream for ``job`` without writing video.mp4 to disk."""
    from .media import open_recording_memory_stream

    resolve_error = _resolve_job_media(client, job)
    if resolve_error:
        return None, DownloadResult(path=None, error=resolve_error)

    stream, result = open_recording_memory_stream(client, job.recording, progress=progress)
    if stream is None and result.http_status in EXPIRED_MEDIA_STATUSES:
        job.recording.mp4_url = ""
        job.recording.m3u8_url = ""
        job.recording.unavailable_reason = ""
        try:
            resolve_media(client, job.recording)
        except Exception as exc:
            return None, DownloadResult(path=None, error=f"resolve failed: {type(exc).__name__}")
        stream, result = open_recording_memory_stream(client, job.recording, progress=progress)
    if stream is not None:
        job.directory.mkdir(parents=True, exist_ok=True)
        (job.directory / "recording.json").write_text(
            json.dumps(job.recording.model_dump(), ensure_ascii=False, indent=1), "utf-8"
        )
    return stream, result


def stream_extract_keyframes_job(
    client: httpx.Client, job: RecordingJob, out_dir: Path, progress=None
) -> list[dict]:
    """Extract keyframes directly from an in-memory lecture stream without writing video.mp4 to disk."""
    from .media import _decode_stream_audio_and_keyframes

    stream, open_res = _open_job_memory_stream(client, job, progress=progress)
    if stream is None:
        raise RuntimeError(open_res.error or "无法读取教学网录像流")
    with stream:
        _, keyframes = _decode_stream_audio_and_keyframes(
            stream, out_dir, need_audio=False, need_keyframes=True, progress=progress
        )
    return keyframes


def stream_process_job(
    client: httpx.Client,
    job: RecordingJob,
    settings,
    *,
    include_keyframes: bool = True,
    progress=None,
    direct_oss: bool = False,
    source_paths: list[Path] | None = None,
    source_failures: list[dict] | None = None,
    stage=None,
) -> ProcessResult:
    """Stream a lecture in RAM to transcribe, extract keyframes, and write notes with 0 video/audio bytes on disk."""
    import time
    from .media import _decode_stream_audio_and_keyframes
    from .transcription import _NoAudioTrack, _decoder_note, _detail, transcribe_memory_parts

    result = ProcessResult(job=job)
    transcript_path = job.directory / "transcript.json"
    keyframes_dir = job.directory / "keyframes"
    need_audio = not transcript_path.exists()
    need_keyframes = bool(include_keyframes and not (keyframes_dir / "index.json").exists())

    audio_parts = []
    keyframes: list[dict] = []
    stream_signature = ""
    started = time.perf_counter()

    if need_audio or need_keyframes:
        if stage:
            stage("正在流式读取录像并提取音轨与画面")
        stream, open_res = _open_job_memory_stream(client, job, progress=progress)
        if stream is None:
            result.errors.append(f"stream: {open_res.error or '录像流打开失败'}")
            return result
        stream_signature = getattr(stream, "signature", "")
        try:
            with stream:
                audio_parts, keyframes = _decode_stream_audio_and_keyframes(
                    stream,
                    keyframes_dir if include_keyframes else None,
                    need_audio=need_audio,
                    need_keyframes=include_keyframes,
                    progress=progress,
                )
        except _NoAudioTrack:
            result.errors.append(
                f"transcribe: 云端转写在该录像里找不到音轨：{job.recording.title}。"
                f"请确认录像完整后重试{_decoder_note()}。"
            )
            return result
        except Exception as exc:
            result.errors.append(
                f"transcribe: 云端转写无法读取该录像的音轨：{_detail(exc)}。"
                f"请确认网络连接稳定后重试{_decoder_note()}。"
            )
            return result

    transcript: dict = {}
    try:
        if transcript_path.exists():
            transcript = json.loads(transcript_path.read_text("utf-8"))
        else:
            if stage:
                stage("上传音频并转写")
            transcript = transcribe_memory_parts(
                audio_parts,
                transcript_path,
                settings,
                video=job.video,
                stream_signature=stream_signature,
                progress=progress,
                direct_oss=direct_oss,
                elapsed_start=started,
            )
        result.transcribed = True
    except Exception as exc:
        result.errors.append(f"transcribe: {exc}")
    finally:
        audio_parts.clear()

    if include_keyframes and result.transcribed:
        if not keyframes and (keyframes_dir / "index.json").exists():
            try:
                raw_kf = json.loads((keyframes_dir / "index.json").read_text("utf-8"))
                keyframes = raw_kf if isinstance(raw_kf, list) else (raw_kf.get("keyframes", []) if isinstance(raw_kf, dict) else [])
            except (OSError, ValueError):
                keyframes = []
        result.keyframes = len(keyframes)

    if keyframes and (job.course_dir / "materials" / "index.json").is_file():
        try:
            from .lecture_source_download import keyframe_source_paths, match_materials_by_keyframes
            from .panel.recordings_api import recording_id
            key = recording_id(job)
            match_materials_by_keyframes(job, key, settings)
            matched_paths = keyframe_source_paths(job, key)
            if matched_paths:
                source_paths = list(dict.fromkeys([*(source_paths or []), *matched_paths]))
        except Exception:
            pass  # Optional visual matching must not interrupt note processing.

    if transcript:
        try:
            notes_path = job.directory / "notes.md"
            from .transcript_notes import restore_saved_note
            restore_saved_note(notes_path, transcript)
            source_context = None
            if not notes_path.exists():
                source_context = collect_note_sources(
                    job.course_dir, job.recording.date, job.recording.title,
                    allowed_paths=source_paths,
                )
                source_context.extend(source_failures or [])
            if stage:
                stage("生成课堂笔记")
            notes = write_notes(
                transcript,
                keyframes,
                notes_path,
                settings,
                title=job.label,
                source_context=source_context,
                progress=progress,
                course_directory=job.course_dir,
            )
            result.notes = notes is not None
        except Exception as exc:
            result.errors.append(f"notes: {exc}")

    return result


def process_job(job: RecordingJob, settings, *, include_keyframes: bool = True, progress=None,
                direct_oss: bool = False, source_paths: list[Path] | None = None,
                source_failures: list[dict] | None = None,
                preserve_video: bool = False) -> ProcessResult:
    """Transcribe, extract keyframes and write notes for one downloaded video."""
    result = ProcessResult(job=job)
    if not job.video.exists():
        result.errors.append("video not downloaded")
        return result

    transcript: dict = {}
    try:
        transcript = transcribe(job.video, job.directory / "transcript.json", settings, progress=progress, direct_oss=direct_oss)
        result.transcribed = True
    except Exception as exc:
        result.errors.append(f"transcribe: {exc}")

    keyframes: list[dict] = []
    if include_keyframes and result.transcribed:
        try:
            keyframes = extract_keyframes(job.video, job.directory / "keyframes")
            result.keyframes = len(keyframes)
        except Exception as exc:
            result.errors.append(f"keyframes: {exc}")

    if keyframes and (job.course_dir / "materials" / "index.json").is_file():
        try:
            from .lecture_source_download import match_materials_by_keyframes, keyframe_source_paths
            from .panel.recordings_api import recording_id
            key = recording_id(job)
            match_materials_by_keyframes(job, key, settings)
            matched_paths = keyframe_source_paths(job, key)
            if matched_paths:
                source_paths = list(dict.fromkeys([*(source_paths or []), *matched_paths]))
        except Exception:
            pass  # Optional visual matching must not interrupt note processing.

    if transcript:
        try:
            notes_path = job.directory / "notes.md"
            from .transcript_notes import restore_saved_note
            restore_saved_note(notes_path, transcript)
            source_context = None
            if not notes_path.exists():
                source_context = collect_note_sources(
                    job.course_dir, job.recording.date, job.recording.title,
                    allowed_paths=source_paths,
                )
                source_context.extend(source_failures or [])
            notes = write_notes(
                transcript,
                keyframes,
                notes_path,
                settings,
                title=job.label,
                source_context=source_context,
                progress=progress,
                course_directory=job.course_dir,
            )
            result.notes = notes is not None
        except Exception as exc:
            result.errors.append(f"notes: {exc}")

    if (settings.delete_video_after_processing
            and not getattr(settings, "save_recordings_locally", False)
            and result.transcribed and result.notes
            and not result.errors and not (job.directory / ".keep-video").exists()):
        if not preserve_video:
            job.video.unlink(missing_ok=True)
            result.removed_video = True
    return result

