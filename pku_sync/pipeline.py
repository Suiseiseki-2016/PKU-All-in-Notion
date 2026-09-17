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
from pathlib import Path

import httpx

from .media import DownloadResult, download_recording, extract_keyframes, write_notes
from .models import Recording
from .recordings import resolve_media
from .store import safe_name
from .transcription import transcribe


@dataclass
class RecordingJob:
    course_name: str
    course_dir: Path
    recording: Recording

    @property
    def directory(self) -> Path:
        return self.course_dir / "recordings" / safe_name(self.recording.slug, "recording")

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
        for entry in entries:
            recording = Recording.model_validate(entry)
            if course_filter and recording.course_id != course_filter:
                continue
            jobs.append(RecordingJob(course_name, course_dir, recording))
    return jobs


def _course_name(course_dir: Path) -> str:
    meta = course_dir / "course.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text("utf-8")).get("name") or course_dir.name
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


def download_job(client: httpx.Client, job: RecordingJob) -> DownloadResult:
    """Download one recording, re-resolving its URL first.

    The media host signs playback URLs with a short lifetime, so a URL stored by
    an earlier `sync` is usually stale by the time a download starts.
    """
    try:
        resolve_media(client, job.recording)
    except Exception as exc:
        return DownloadResult(path=None, error=f"resolve failed: {exc}")

    result = download_recording(client, job.recording, job.directory)
    if result.path is not None:
        (job.directory / "recording.json").write_text(
            json.dumps(job.recording.model_dump(), ensure_ascii=False, indent=1), "utf-8"
        )
    return result


def process_job(job: RecordingJob, settings) -> ProcessResult:
    """Transcribe, extract keyframes and write notes for one downloaded video."""
    result = ProcessResult(job=job)
    if not job.video.exists():
        result.errors.append("video not downloaded")
        return result

    transcript: dict = {}
    try:
        transcript = transcribe(job.video, job.directory / "transcript.json", settings)
        result.transcribed = True
    except Exception as exc:
        result.errors.append(f"transcribe: {exc}")

    keyframes: list[dict] = []
    try:
        keyframes = extract_keyframes(job.video, job.directory / "keyframes")
        result.keyframes = len(keyframes)
    except Exception as exc:
        result.errors.append(f"keyframes: {exc}")

    if transcript:
        try:
            notes = write_notes(
                transcript,
                keyframes,
                job.directory / "notes.md",
                settings,
                title=job.label,
            )
            result.notes = notes is not None
        except Exception as exc:
            result.errors.append(f"notes: {exc}")

    if settings.delete_video_after_processing and result.transcribed and not result.errors:
        job.video.unlink(missing_ok=True)
        result.removed_video = True
    return result
