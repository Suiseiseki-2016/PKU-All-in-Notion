"""Transcription backends: local faster-whisper, or our cloud wrapper (M2).

``pipeline.process_job`` stays backend-agnostic: it asks this module to turn
a video file into ``transcript.json``. The local backend wraps
``media.transcribe`` (faster-whisper on this machine). The cloud backend is
the client half of the M2 wrapper service (docs/SERVICE_PLAN.md §1.7,
§3.2-3.3): it extracts a 16 kHz mono audio track, uploads it to OUR relay
with the platform account session (§1.7: users never see any key), and
writes the returned transcript. The relay -- not the user -- holds the
upstream ASR vendor key and meters minutes per platform account;
classmates register no third-party account. Audio leaves the machine only
toward the relay, never toward a vendor directly (§3.2).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


def transcribe(video: Path, target: Path, settings: "Settings") -> dict:
    """Dispatch one recording to the configured transcription backend.

    Mirrors ``media.transcribe``'s contract (writes ``target``, returns the
    transcript payload) so callers stay unaware of which backend ran.
    """
    backend = (settings.transcription_backend or "local").strip().lower()
    if backend == "local":
        from .media import transcribe as local_transcribe

        return local_transcribe(video, target, settings)
    if backend == "cloud":
        return transcribe_cloud(video, target, settings)
    raise ValueError(f"unknown transcription backend: {settings.transcription_backend!r}")


def _extract_audio(video: Path, out: Path) -> Path:
    """16 kHz mono AAC track: §3.2 uploads audio only, never the video."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "cloud transcription needs ffmpeg to extract the audio track; "
            "install ffmpeg or set TRANSCRIPTION_BACKEND=local"
        )
    subprocess.run(
        [ffmpeg, "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(out)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return out


def _upload(url: str, key: str, audio: Path) -> dict:
    """POST the audio to the relay; the relay answers with the transcript."""
    import httpx

    try:
        with httpx.Client(timeout=600.0) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                files={"audio": (audio.name, audio.read_bytes(), "audio/mp4")},
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"cloud transcription upload failed: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(
            f"cloud transcription rejected the upload (HTTP {response.status_code}): "
            f"{(response.text or '')[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"cloud transcription returned non-JSON: {(response.text or '')[:200]}"
        ) from exc
    if not isinstance(payload, dict) or "segments" not in payload:
        raise RuntimeError("cloud transcription response is missing 'segments'")
    return payload


def transcribe_cloud(video: Path, target: Path, settings: "Settings") -> dict:
    """Extract the audio track, upload it, write the returned transcript."""
    url = (getattr(settings, "cloud_transcribe_url", "") or "").strip()
    token = (getattr(settings, "platform_token", "") or "").strip()
    if not url:
        raise RuntimeError(
            "cloud transcription needs CLOUD_TRANSCRIBE_URL (the M2 relay, "
            "docs/SERVICE_PLAN.md §3.3); set TRANSCRIPTION_BACKEND=local for now"
        )
    if not token:
        raise RuntimeError(
            "cloud transcription needs PLATFORM_TOKEN (the platform account "
            "session, written automatically at activation; "
            "docs/SERVICE_PLAN.md §1.7); set TRANSCRIPTION_BACKEND=local for now"
        )
    with tempfile.TemporaryDirectory(prefix="pku-sync-audio-") as tmp:
        audio = _extract_audio(video, Path(tmp) / "audio.m4a")
        payload = _upload(url, token, audio)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    return payload
