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
import hashlib
import os
from fractions import Fraction
from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


def transcribe(video: Path, target: Path, settings: "Settings", progress=None, direct_oss: bool = False) -> dict:
    """Dispatch one recording to the configured transcription backend.

    Mirrors ``media.transcribe``'s contract (writes ``target``, returns the
    transcript payload) so callers stay unaware of which backend ran.
    """
    backend = (settings.transcription_backend or "cloud").strip().lower()
    if os.environ.get("PKU_DESKTOP_APP") == "1" and backend != "cloud":
        raise RuntimeError("桌面版仅支持云端转写；请登录平台账号后重试。")
    if target.exists():
        return json.loads(target.read_text("utf-8"))
    if backend == "local":
        from .media import transcribe as local_transcribe

        return local_transcribe(video, target, settings)
    if backend == "cloud":
        return transcribe_cloud(video, target, settings, progress=progress, direct_oss=direct_oss)
    raise ValueError(f"unknown transcription backend: {settings.transcription_backend!r}")


# What the relay's ASR vendor wants, and the cheapest useful shape.
_AUDIO_CODEC = "aac"
_AUDIO_RATE = 16000
_AUDIO_LAYOUT = "mono"
_CHUNK_SECONDS = 600
_AUDIO_PARTS_VERSION = 2


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
            f"请确认录像文件完整，重新下载该录像后重试{_decoder_note()}。"
        ) from exc
    return out


def _audio_duration(video: Path) -> float:
    """Return container audio duration, or zero when metadata is unavailable."""
    try:
        import av

        with av.open(str(video)) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is not None and stream.duration and stream.time_base:
                return float(stream.duration * stream.time_base)
    except Exception:
        # Extraction below provides the useful error for missing or broken media.
        pass
    return 0.0


class MemoryAudioPart:
    """In-memory encoded 16 kHz mono AAC (.m4a) chunk — never touches disk."""

    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self.stem = Path(name).stem
        self.suffix = Path(name).suffix
        self._data = data

    @property
    def data(self) -> bytes:
        return self._data

    def size_bytes(self) -> int:
        return len(self._data)

    def open(self, mode: str = "rb"):
        import io
        return io.BytesIO(self._data)


class _MemoryAudioChunkEncoder:
    """Stateful 16 kHz mono AAC (.m4a) chunk encoder that muxes entirely into RAM."""

    def __init__(self, chunk_seconds: int = _CHUNK_SECONDS, progress=None, total_samples: int = 0) -> None:
        import av
        from av.audio.fifo import AudioFifo
        from av.audio.resampler import AudioResampler

        if type(chunk_seconds) is not int or chunk_seconds <= 0:
            raise ValueError("音频分段长度必须是正整数秒。")
        self._av = av
        self._limit = chunk_seconds * _AUDIO_RATE
        self._progress = progress
        self._total_samples = total_samples
        self._fifo = AudioFifo()
        self._resampler = AudioResampler(format="fltp", layout=_AUDIO_LAYOUT, rate=_AUDIO_RATE)
        self._buf = None
        self._sink = None
        self._track = None
        self._part_offset = 0.0
        self._samples = 0
        self._start = 0
        self._last_report = 0
        self.parts: list[tuple[MemoryAudioPart, float]] = []
        if self._progress:
            self._progress(0, self._total_samples, "audio_extract", 0)

    def _open_sink(self) -> None:
        import io
        self._buf = io.BytesIO()
        self._part_offset = self._start / _AUDIO_RATE
        self._sink = self._av.open(
            self._buf, mode="w", format="mp4",
            options={"movie_timescale": str(_AUDIO_RATE)},
        )
        self._track = self._sink.add_stream(_AUDIO_CODEC, rate=_AUDIO_RATE)
        self._track.layout = _AUDIO_LAYOUT

    def _close_sink(self) -> None:
        if self._sink is None:
            return
        for packet in self._track.encode(None):
            self._sink.mux(packet)
        self._sink.close()
        name = f"part-{len(self.parts):04d}.m4a"
        self.parts.append((MemoryAudioPart(name, self._buf.getvalue()), self._part_offset))
        self._sink = None
        self._track = None
        self._buf = None

    def _write_resampled(self, frame) -> None:
        frame.pts = None
        self._fifo.write(frame)
        while self._fifo.samples:
            if self._sink is None:
                self._open_sink()
            piece = self._fifo.read(min(self._fifo.samples, self._limit - (self._samples - self._start)))
            piece.pts = self._samples - self._start
            piece.time_base = Fraction(1, _AUDIO_RATE)
            for packet in self._track.encode(piece):
                self._sink.mux(packet)
            self._samples += piece.samples
            if self._progress and self._samples - self._last_report >= 30 * _AUDIO_RATE:
                self._progress(
                    min(self._samples, self._total_samples) if self._total_samples else self._samples,
                    self._total_samples, "audio_extract", 0,
                )
                self._last_report = self._samples
            if self._samples - self._start == self._limit:
                self._close_sink()
                self._start = self._samples

    def feed(self, frame) -> None:
        for resampled in self._resampler.resample(frame):
            self._write_resampled(resampled)

    def finish(self) -> list[tuple[MemoryAudioPart, float]]:
        try:
            for resampled in self._resampler.resample(None):
                self._write_resampled(resampled)
            if self._sink is not None:
                self._close_sink()
            if self._progress:
                total = self._total_samples or self._samples
                self._progress(total, total, "audio_extract", 0)
        finally:
            if self._sink is not None:
                try:
                    self._sink.close()
                except Exception:
                    pass
                self._sink = None
                self._track = None
                self._buf = None
        return self.parts

    def close(self) -> None:
        if self._sink is not None:
            try:
                self._sink.close()
            except Exception:
                pass
            self._sink = None
            self._track = None
            self._buf = None


def _encode_audio_parts_in_memory(
    frames, chunk_seconds: int = _CHUNK_SECONDS, progress=None, total_samples: int = 0,
) -> list[tuple[MemoryAudioPart, float]]:
    """Encode resampled audio frames into in-memory .m4a chunks without touching disk."""
    encoder = _MemoryAudioChunkEncoder(chunk_seconds=chunk_seconds, progress=progress, total_samples=total_samples)
    try:
        for frame in frames:
            encoder.feed(frame)
        return encoder.finish()
    finally:
        encoder.close()


def _extract_audio_parts_in_memory(
    video: Path, chunk_seconds: int = _CHUNK_SECONDS, progress=None,
) -> list[tuple[MemoryAudioPart, float]]:
    """Decode audio from ``video`` and encode 16 kHz mono AAC parts purely in RAM."""
    try:
        import av
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
            total_samples = (int(float(source.duration * source.time_base) * _AUDIO_RATE)
                             if source.duration and source.time_base else 0)
            parts = _encode_audio_parts_in_memory(
                container.decode(source), chunk_seconds, progress=progress, total_samples=total_samples
            )
            if not parts:
                raise _NoAudioTrack(video.name)
            return parts
    except _NoAudioTrack as exc:
        raise RuntimeError(
            f"云端转写在该录像里找不到音轨：{exc.args[0]}。"
            f"请确认录像下载完整，或重新下载该录像后重试{_decoder_note()}。"
        ) from None
    except Exception as exc:
        raise RuntimeError(
            f"云端转写无法读取该录像的音轨：{_detail(exc)}。"
            f"请确认录像文件完整，重新下载该录像后重试{_decoder_note()}。"
        ) from exc


def _encode_audio_parts(frames, directory: Path, chunk_seconds: int, progress=None,
                        total_samples: int = 0) -> list[tuple[Path, float]]:
    """Bound each part by resampled samples, with timestamps local to the part."""
    import av
    from av.audio.fifo import AudioFifo
    from av.audio.resampler import AudioResampler

    if type(chunk_seconds) is not int or chunk_seconds <= 0:
        raise ValueError("音频分段长度必须是正整数秒。")
    limit = chunk_seconds * _AUDIO_RATE
    directory.mkdir(parents=True, exist_ok=True)
    parts = []
    fifo = AudioFifo()
    resampler = AudioResampler(format="fltp", layout=_AUDIO_LAYOUT, rate=_AUDIO_RATE)
    sink, track = None, None
    samples, start, last_report = 0, 0, 0
    if progress:
        progress(0, total_samples, "audio_extract", 0)

    def write_resampled(frame):
        nonlocal sink, track, samples, start, last_report
        # Source timestamp gaps must not turn into extra billed silence.
        frame.pts = None
        fifo.write(frame)
        while fifo.samples:
            if sink is None:
                path = directory / f"part-{len(parts):04d}.m4a"
                # MP4's default movie timescale rounds edit-list duration to
                # milliseconds. Preserve the exact 16 kHz sample boundary.
                sink = av.open(str(path), mode="w", options={"movie_timescale": str(_AUDIO_RATE)})
                track = sink.add_stream(_AUDIO_CODEC, rate=_AUDIO_RATE)
                track.layout = _AUDIO_LAYOUT
                parts.append((path, start / _AUDIO_RATE))
            piece = fifo.read(min(fifo.samples, limit - (samples - start)))
            piece.pts = samples - start
            piece.time_base = Fraction(1, _AUDIO_RATE)
            for packet in track.encode(piece):
                sink.mux(packet)
            samples += piece.samples
            if progress and samples - last_report >= 30 * _AUDIO_RATE:
                progress(min(samples, total_samples) if total_samples else samples,
                         total_samples, "audio_extract", 0)
                last_report = samples
            if samples - start == limit:
                for packet in track.encode(None):
                    sink.mux(packet)
                sink.close()
                sink = None
                start = samples

    try:
        for frame in frames:
            for resampled in resampler.resample(frame):
                write_resampled(resampled)
        for resampled in resampler.resample(None):
            write_resampled(resampled)
        if sink is not None:
            for packet in track.encode(None):
                sink.mux(packet)
            sink.close()
            sink = None
        if progress:
            progress(total_samples or samples, total_samples or samples, "audio_extract", 0)
    finally:
        if sink is not None:
            sink.close()
    return parts


def _extract_audio_parts(video: Path, directory: Path, chunk_seconds: int, progress=None) -> list[tuple[Path, float]]:
    """Decode once and encode upload-sized AAC parts directly from a long video.

    The old long-recording path encoded one complete audio file and then
    decoded/re-encoded it into parts. Keeping only a single part open also
    avoids a full-length temporary audio file alongside all the parts.
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
            total_samples = (int(float(source.duration * source.time_base) * _AUDIO_RATE)
                             if source.duration and source.time_base else 0)
            return _encode_audio_parts(container.decode(source), directory, chunk_seconds,
                                       progress, total_samples)
    except _NoAudioTrack as exc:
        raise RuntimeError(
            f"云端转写在该录像里找不到音轨：{exc.args[0]}。"
            f"请确认录像下载完整，或重新下载该录像后重试{_decoder_note()}。"
        ) from None
    except Exception as exc:
        raise RuntimeError(
            f"云端转写无法读取该录像的音轨：{_detail(exc)}。"
            f"请确认录像文件完整，重新下载该录像后重试{_decoder_note()}。"
        ) from exc


_RETRY_DELAY = 2.0  # covers an edge/failover reconnect window; tests zero it


def _part_index_from_audio(audio: Path | MemoryAudioPart) -> int:
    name = audio.stem  # e.g. "part-0002" or "audio"
    if name.startswith("part-"):
        suffix = name[5:]
        if suffix.isdigit():
            return int(suffix)
    return 0


def _audio_size_bytes(audio: Path | MemoryAudioPart) -> int:
    if isinstance(audio, MemoryAudioPart):
        return audio.size_bytes()
    return audio.stat().st_size


def _transcribe_headers(key: str, audio: Path | MemoryAudioPart, *, source_id: str | None = None, transcribe_mode: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}"}
    if source_id:
        headers["X-PKU-Source-Id"] = str(source_id)
        headers["X-PKU-Part-Index"] = str(_part_index_from_audio(audio))
    if transcribe_mode in {"basic", "advanced"}:
        headers["X-PKU-Transcribe-Mode"] = transcribe_mode
    return headers


def _upload(url: str, key: str, audio: Path | MemoryAudioPart, *, source_id: str | None = None, transcribe_mode: str | None = None) -> dict:
    """POST the audio to the relay; the relay answers with the transcript.

    Retry one transport failure or transient upstream 502/503/504 response.
    Authentication, quota and rate-limit responses remain terminal.
    """
    import httpx

    req_headers = _transcribe_headers(key, audio, source_id=source_id, transcribe_mode=transcribe_mode)
    for attempt in (1, 2):
        try:
            from .network import dead_loopback_proxy

            with audio.open("rb") as source, httpx.Client(timeout=600.0, trust_env=not dead_loopback_proxy()) as client:
                response = client.post(
                    url,
                    headers=req_headers,
                    files={"audio": (audio.name, source, "audio/mp4")},
                )
            if response.status_code in (502, 503, 504) and attempt == 1:
                time.sleep(_RETRY_DELAY)
                continue
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
        reasons = {
            401: "登录已失效，请重新登录",
            402: "转写额度不足，请兑换额度后重试",
            403: "请先验证邮箱",
            413: "这一段音频过大，暂时无法上传",
            429: "请求过于频繁，请稍后重试",
            502: "云端识别服务暂时失败",
            503: "云端识别服务暂不可用",
        }
        reason = reasons.get(response.status_code, "云端服务暂时无法处理")
        raise RuntimeError(f"cloud transcription rejected the upload (HTTP {response.status_code}): {reason}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"cloud transcription returned non-JSON: {(response.text or '')[:200]}"
        ) from exc
    if not isinstance(payload, dict) or "segments" not in payload:
        raise RuntimeError("cloud transcription response is missing 'segments'")
    return payload


def _upload_direct(url: str, key: str, audio: Path | MemoryAudioPart, *, source_id=None, transcribe_mode: str | None = None) -> dict:
    """With explicit consent, send this audio part straight to private OSS."""
    import httpx
    from urllib.parse import urlsplit
    from .network import dead_loopback_proxy

    suffix = "/v1/transcribe"
    if not url.rstrip("/").endswith(suffix):
        raise RuntimeError("云端直传地址配置不正确。")
    base = url.rstrip("/")[:-len(suffix)]
    headers = _transcribe_headers(key, audio, source_id=source_id, transcribe_mode=transcribe_mode)
    size_bytes = _audio_size_bytes(audio)
    for cycle in (1, 2):
        try:
            with httpx.Client(trust_env=not dead_loopback_proxy()) as client:
                body = {'size_bytes': size_bytes}
                if source_id is not None:
                    body['source_id'] = source_id
                    body['part_index'] = _part_index_from_audio(audio)
                if transcribe_mode is not None:
                    body['transcribe_mode'] = transcribe_mode
                issued = client.post(base + suffix + "/direct/init", headers=headers,
                                     json=body, timeout=30)
                if issued.status_code != 200:
                    raise RuntimeError(f"音频直传授权失败（HTTP {issued.status_code}）；请检查登录与额度。")
                info = issued.json()
                put_url = info.get("upload_url", "")
                host = urlsplit(put_url).hostname or ""
                if urlsplit(put_url).scheme != "https" or not host.endswith(".aliyuncs.com"):
                    raise RuntimeError("云端返回的音频上传地址无效。")
                upload_id = info.get("upload_id", "")
                if not isinstance(upload_id, str) or len(upload_id) != 32:
                    raise RuntimeError("云端返回的上传授权无效。")
                with audio.open("rb") as source:
                    uploaded = client.put(put_url, content=source,
                                          headers={"Content-Type": "audio/mp4", "Content-Length": str(size_bytes)},
                                          timeout=600)
                if uploaded.status_code >= 500 and cycle == 1:
                    time.sleep(_RETRY_DELAY)
                    continue
                if uploaded.status_code not in (200, 201):
                    raise RuntimeError(f"音频直传到 OSS 失败（HTTP {uploaded.status_code}）；请检查网络后重试。")
                completed = client.post(base + suffix + "/direct/complete", headers=headers,
                                        json={"upload_id": upload_id}, timeout=1900)
                # 408/429 are throttle or timeout signals, 409 marks an
                # in-flight complete the server is still finishing, and
                # 5xx/52x covers gateway and Cloudflare-style failovers.
                if completed.status_code in (408, 409, 429, 500, 502, 503, 504, 520, 522, 524) and cycle == 1:
                    time.sleep(_RETRY_DELAY)
                    continue
                if completed.status_code != 200:
                    raise RuntimeError(f"云端直传转写失败（HTTP {completed.status_code}）；已完成片段会在重试时复用。")
                payload = completed.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
                    raise RuntimeError("云端直传返回的转写内容无效。")
                return payload
        except httpx.HTTPError as exc:
            if cycle == 1:
                time.sleep(_RETRY_DELAY)
                continue
            raise RuntimeError("音频直传连接中断；请检查网络后重试。") from exc


def transcribe_memory_parts(
    parts: list[tuple[MemoryAudioPart | Path, float]],
    target: Path,
    settings: "Settings",
    *,
    video: Path | None = None,
    stream_signature: str = "",
    progress=None,
    direct_oss: bool = False,
    elapsed_start: float | None = None,
) -> dict:
    """Upload in-memory encoded AAC parts and write ``target`` without touching disk for audio."""
    started = elapsed_start if elapsed_start is not None else time.perf_counter()
    url = (getattr(settings, "cloud_transcribe_url", "") or "").strip()
    token = (getattr(settings, "platform_token", "") or "").strip()
    if not url:
        raise RuntimeError(
            "云端转写服务地址缺失（CLOUD_TRANSCRIBE_URL）；请检查安装或服务配置。"
        )
    if not token:
        raise RuntimeError(
            "云端转写需要平台登录；请登录账号后重试（PLATFORM_TOKEN 缺失）。"
        )
    if not parts:
        raise RuntimeError(
            f"云端转写在该录像里找不到音轨。"
            f"请确认录像完整后重试{_decoder_note()}。"
        )
    if progress:
        progress(0, len(parts), "audio_parts", 0)
    uploader = _upload_direct if direct_oss else _upload
    source_id = getattr(settings, '_metered_operation_context', {}).get('source_id')
    strategy = getattr(settings, 'notes_strategy', None)
    transcribe_mode = "advanced" if strategy == "topics" else "basic"
    if (source_id or transcribe_mode == "advanced") and uploader in (_upload, _upload_direct):
        from functools import partial
        uploader = partial(uploader, source_id=source_id, transcribe_mode=transcribe_mode)
    video_ref = video if video is not None else (target.parent / "video.mp4")
    if len(parts) == 1:
        payload = uploader(url, token, parts[0][0])
        if progress:
            progress(1, 1, "audio_parts", 0)
    else:
        payload = _transcribe_parts(
            parts, url, token, video_ref, target,
            progress=progress, uploader=uploader, stream_signature=stream_signature,
        )
    payload = dict(payload)
    payload["elapsed"] = round(time.perf_counter() - started, 1)
    _atomic_json(target, payload)
    if len(parts) > 1:
        _clear_part_cache(video_ref, target, len(parts), stream_signature=stream_signature)
    return payload


def transcribe_cloud(video: Path, target: Path, settings: "Settings", progress=None, direct_oss: bool = False) -> dict:
    """Extract the audio track, upload it, write the returned transcript."""
    # The cloud backend is the shipping default, but it recorded no wall time,
    # so "how long does one lecture take" had no answer for it. The local
    # backend already records ``elapsed``; record it here too.
    started = time.perf_counter()
    url = (getattr(settings, "cloud_transcribe_url", "") or "").strip()
    token = (getattr(settings, "platform_token", "") or "").strip()
    if not url:
        raise RuntimeError(
            "云端转写服务地址缺失（CLOUD_TRANSCRIBE_URL）；请检查安装或服务配置。"
        )
    if not token:
        raise RuntimeError(
            "云端转写需要平台登录；请登录账号后重试（PLATFORM_TOKEN 缺失）。"
        )
    # In production, encode 16 kHz mono AAC chunks directly in RAM (io.BytesIO)
    # so temporary .m4a files never touch the user's disk. Fall back to the
    # TemporaryDirectory path only when a unit test monkeypatches the file-based
    # extraction or upload hooks.
    if (_upload is _DEFAULT_UPLOAD
            and _upload_direct is _DEFAULT_UPLOAD_DIRECT
            and _extract_audio is _DEFAULT_EXTRACT_AUDIO
            and _extract_audio_parts is _DEFAULT_EXTRACT_AUDIO_PARTS
            and _split_audio is _DEFAULT_SPLIT_AUDIO):
        parts = _extract_audio_parts_in_memory(video, _CHUNK_SECONDS, progress=progress)
        return transcribe_memory_parts(
            parts, target, settings, video=video, progress=progress,
            direct_oss=direct_oss, elapsed_start=started,
        )

    with tempfile.TemporaryDirectory(prefix="pku-sync-audio-") as tmp:
        if _audio_duration(video) > _CHUNK_SECONDS:
            parts = _extract_audio_parts(video, Path(tmp), _CHUNK_SECONDS, progress=progress)
        else:
            audio = _extract_audio(video, Path(tmp) / "audio.m4a")
            parts = _split_audio(audio, Path(tmp), _CHUNK_SECONDS)
        if progress:
            progress(0, len(parts), "audio_parts", 0)
        uploader = _upload_direct if direct_oss else _upload
        source_id = getattr(settings, '_metered_operation_context', {}).get('source_id')
        strategy = getattr(settings, 'notes_strategy', None)
        transcribe_mode = "advanced" if strategy == "topics" else "basic"
        if (source_id or transcribe_mode == "advanced") and uploader in (_upload, _upload_direct):
            from functools import partial
            uploader = partial(uploader, source_id=source_id, transcribe_mode=transcribe_mode)
        if len(parts) == 1:
            payload = uploader(url, token, parts[0][0])
            if progress:
                progress(1, 1, "audio_parts", 0)
        else:
            payload = _transcribe_parts(parts, url, token, video, target, progress=progress, uploader=uploader)
    # Keep the same ``elapsed`` contract as the local backend.  This is
    # wall-clock time for extraction plus the cloud request(s), not audio
    # duration or billable seconds.
    payload = dict(payload)
    payload["elapsed"] = round(time.perf_counter() - started, 1)
    _atomic_json(target, payload)
    if len(parts) > 1:
        _clear_part_cache(video, target, len(parts))
    return payload


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    temporary.replace(path)


def _split_audio(audio: Path, directory: Path, chunk_seconds: int) -> list[tuple[Path, float]]:
    """Split a long extracted audio track without requiring an external ffmpeg."""
    import av

    with av.open(str(audio)) as source:
        stream = next(s for s in source.streams if s.type == "audio")
        duration = float(stream.duration * stream.time_base) if stream.duration else 0
        if duration <= chunk_seconds:
            return [(audio, 0.0)]
        return _encode_audio_parts(source.decode(stream), directory, chunk_seconds) or [(audio, 0.0)]


def _part_cache_signature(
    parts: list[tuple[Path | MemoryAudioPart, float]],
    video: Path | None,
    stream_signature: str = "",
) -> str:
    if stream_signature:
        return hashlib.sha256(f"{stream_signature}:{_CHUNK_SECONDS}:{_AUDIO_PARTS_VERSION}".encode()).hexdigest()[:16]
    if video is not None and video.exists():
        stat = video.stat()
        return hashlib.sha256(f"{stat.st_size}:{stat.st_mtime_ns}:{_CHUNK_SECONDS}:{_AUDIO_PARTS_VERSION}".encode()).hexdigest()[:16]
    digest = hashlib.sha256(f"{_CHUNK_SECONDS}:{_AUDIO_PARTS_VERSION}".encode())
    for item, offset in parts:
        digest.update(f":{offset}:".encode())
        if isinstance(item, MemoryAudioPart):
            digest.update(hashlib.sha256(item.data).digest())
        elif isinstance(item, Path) and item.exists():
            stat = item.stat()
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()[:16]


def _transcribe_parts(
    parts: list[tuple[Path | MemoryAudioPart, float]], url: str, token: str, video: Path, target: Path,
    progress=None, uploader=None, *, stream_signature: str = "",
) -> dict:
    """Run two uploads at a time; retain successful replies for a later retry."""
    signature = _part_cache_signature(parts, video, stream_signature=stream_signature)
    cache = target.parent / ".transcription-parts"
    cache.mkdir(parents=True, exist_ok=True)
    replies: list[dict | None] = [None] * len(parts)
    pending = []
    for index, (path, _) in enumerate(parts):
        saved = cache / f"{signature}-{index:04d}.json"
        try:
            reply = json.loads(saved.read_text("utf-8"))
            if not isinstance(reply.get("segments"), list):
                raise ValueError("invalid cached part")
            replies[index] = reply
        except (OSError, ValueError, TypeError, AttributeError):
            pending.append((index, path, saved))
    failures = []
    uploader = uploader or _upload
    completed = len(parts) - len(pending)
    if progress:
        progress(completed, len(parts), "audio_parts", 0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(uploader, url, token, path): (index, saved)
                   for index, path, saved in pending}
        for future in as_completed(futures):
            index, saved = futures[future]
            try:
                reply = future.result()
                _atomic_json(saved, reply)
                replies[index] = reply
                completed += 1
                if progress:
                    progress(completed, len(parts), "audio_parts", 0)
            except Exception as exc:
                failures.append((index, exc))
    if failures:
        index, error = min(failures, key=lambda item: item[0])
        clean_error = str(error).rstrip().rstrip("。")
        suffix = "；已完成片段会在重试时复用"
        while clean_error.endswith(suffix):
            clean_error = clean_error[:-len(suffix)].rstrip("。")
        raise RuntimeError(f"第 {index + 1}/{len(parts)} 段转写失败；已完成片段会在重试时复用：{clean_error}") from error
    segments = []
    for reply, (_, offset) in zip(replies, parts):
        for segment in reply["segments"]:
            segment = segment.copy()
            for name in ("start", "end"):
                if isinstance(segment.get(name), (int, float)):
                    segment[name] = round(segment[name] + offset, 3)
            segments.append(segment)
    payload = {"language": replies[0].get("language", "zh"),
               "duration_seconds": round(max((float(seg.get("end", seg.get("start", 0))) for seg in segments), default=0), 3),
               "segments": segments,
               "seconds_charged": sum(reply.get("seconds_charged", 0) for reply in replies),
               "reused": all(reply.get("reused", False) for reply in replies)}
    return payload


def _clear_part_cache(video: Path, target: Path, count: int, *, stream_signature: str = "") -> None:
    cache = target.parent / ".transcription-parts"
    if stream_signature or (video is not None and video.exists()):
        signature = _part_cache_signature([], video, stream_signature=stream_signature)
        for index in range(count):
            (cache / f"{signature}-{index:04d}.json").unlink(missing_ok=True)
    elif cache.is_dir():
        for item in cache.glob("*.json"):
            item.unlink(missing_ok=True)


_DEFAULT_UPLOAD = _upload
_DEFAULT_UPLOAD_DIRECT = _upload_direct
_DEFAULT_EXTRACT_AUDIO = _extract_audio
_DEFAULT_EXTRACT_AUDIO_PARTS = _extract_audio_parts
_DEFAULT_SPLIT_AUDIO = _split_audio

