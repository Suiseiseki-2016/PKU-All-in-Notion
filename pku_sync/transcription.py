"""Transcription backends: local faster-whisper, or our cloud wrapper (M2).

``pipeline.process_job`` stays backend-agnostic: it asks this module to turn
a video file into ``transcript.json``. The local backend wraps
``media.transcribe`` (faster-whisper on this machine). The cloud backend is
the client half of the relay transcription service: it extracts a 16 kHz
mono audio track, uploads it to OUR relay with the platform account session
(users never see any key), and writes the returned transcript. The relay --
not the user -- holds the upstream ASR vendor key and meters minutes per
platform account; classmates register no third-party account. Audio leaves
the machine only toward the relay, never toward a vendor directly.

Extraction decodes inside this process through PyAV, which links its own
FFmpeg libraries, so a packaged install needs no media binary on PATH. The
only remaining use of a system ffmpeg is the HLS download fallback in
``media.py``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
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


# What the relay's ASR vendor wants, and the cheapest useful shape.
_AUDIO_CODEC = "aac"
_AUDIO_RATE = 16000
_AUDIO_LAYOUT = "mono"


class _NoAudioTrack(Exception):
    """The container opened and decoded, but carries no audio stream."""


def _decoder_note() -> str:
    """Support hint naming the decoder that actually runs.

    This path used to shell out to a system ffmpeg, so "install ffmpeg" is
    still the first thing a student (or a support thread) reaches for. Saying
    plainly which decoder is in use is what stops that wrong fix.
    """
    if shutil.which("ffmpeg"):
        return "（本机的 ffmpeg 不参与云端转写：音轨由应用内置解码器 PyAV 处理）"
    return "（无需安装 ffmpeg：音轨由应用内置解码器 PyAV 处理）"


def _detail(exc: Exception) -> str:
    """One-line cause for the student-facing copy — never a stack trace."""
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text if len(text) <= 200 else text[:197] + "..."


def _extract_audio(video: Path, out: Path) -> Path:
    """16 kHz mono AAC track: uploads audio only, never the video.

    Decoding happens in this process. The import is deferred so the panel,
    sync and directory paths never load a media decoder they do not use, and
    so a broken install surfaces here as actionable copy instead of an import
    error at startup.
    """
    try:
        import av
        from av.audio.resampler import AudioResampler
    except Exception as exc:
        raise RuntimeError(
            f"云端转写需要应用内置的音频解码器（PyAV），当前安装里无法加载："
            f"{_detail(exc)}。请重新安装本应用后重试{_decoder_note()}。"
        ) from exc

    try:
        with av.open(str(video)) as container:
            source = next((s for s in container.streams if s.type == "audio"), None)
            if source is None:
                raise _NoAudioTrack(video.name)
            out.parent.mkdir(parents=True, exist_ok=True)
            with av.open(str(out), mode="w") as sink:
                track = sink.add_stream(_AUDIO_CODEC, rate=_AUDIO_RATE)
                track.layout = _AUDIO_LAYOUT
                resampler = AudioResampler(
                    format=track.format.name, layout=_AUDIO_LAYOUT, rate=_AUDIO_RATE
                )
                for frame in container.decode(source):
                    for resampled in resampler.resample(frame):
                        for packet in track.encode(resampled):
                            sink.mux(packet)
                for resampled in resampler.resample(None):
                    for packet in track.encode(resampled):
                        sink.mux(packet)
                for packet in track.encode(None):
                    sink.mux(packet)
    except _NoAudioTrack as exc:
        raise RuntimeError(
            f"云端转写在该录像里找不到音轨：{exc.args[0]}。"
            f"请确认录像下载完整，或重新下载该录像后重试{_decoder_note()}。"
        ) from None
    except Exception as exc:
        raise RuntimeError(
            f"云端转写无法读取该录像的音轨：{_detail(exc)}。"
            f"请确认录像文件完整（可重新下载后重试），或设置 "
            f"TRANSCRIPTION_BACKEND=local 改用本机转写{_decoder_note()}。"
        ) from exc
    return out


_RETRY_DELAY = 2.0  # covers an edge/failover reconnect window; tests zero it


def _upload(url: str, key: str, audio: Path) -> dict:
    """POST the audio to the relay; the relay answers with the transcript.

    One transport-level retry: a failover between relay hosts kills the
    in-flight request once, and that failure is absorbed here. HTTP status
    answers are real answers (quota, auth) -- never retried.
    """
    import httpx

    for attempt in (1, 2):
        try:
            with httpx.Client(timeout=600.0) as client:
                response = client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"audio": (audio.name, audio.read_bytes(), "audio/mp4")},
                )
            break
        except httpx.TransportError as exc:
            if attempt == 2:
                raise RuntimeError(
                    f"cloud transcription upload failed: {exc}"
                ) from exc
            time.sleep(_RETRY_DELAY)
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
            "cloud transcription needs CLOUD_TRANSCRIBE_URL (the relay "
            "transcription service); set TRANSCRIPTION_BACKEND=local for now"
        )
    if not token:
        raise RuntimeError(
            "cloud transcription needs PLATFORM_TOKEN (the platform account "
            "session, written automatically at activation); "
            "set TRANSCRIPTION_BACKEND=local for now"
        )
    with tempfile.TemporaryDirectory(prefix="pku-sync-audio-") as tmp:
        audio = _extract_audio(video, Path(tmp) / "audio.m4a")
        payload = _upload(url, token, audio)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    return payload
