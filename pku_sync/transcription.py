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

    parts: list[tuple[Path, float]] = []
    sink = None
    try:
        with av.open(str(video)) as container:
            source = next((s for s in container.streams if s.type == "audio"), None)
            if source is None:
                raise _NoAudioTrack(video.name)
            directory.mkdir(parents=True, exist_ok=True)
            track = None
            resampler = None
            samples = 0
            start = 0
            total_samples = (int(float(source.duration * source.time_base) * _AUDIO_RATE)
                             if source.duration and source.time_base else 0)
            last_report = 0
            if progress:
                progress(0, total_samples, "audio_extract", 0)

            def write_frame(frame) -> None:
                nonlocal sink, track, start, samples, last_report
                if sink is None:
                    path = directory / f"part-{len(parts):04d}.m4a"
                    sink = av.open(str(path), mode="w")
                    track = sink.add_stream(_AUDIO_CODEC, rate=_AUDIO_RATE)
                    track.layout = _AUDIO_LAYOUT
                    parts.append((path, start / _AUDIO_RATE))
                for packet in track.encode(frame):
                    sink.mux(packet)
                samples += frame.samples
                if progress and samples - last_report >= 30 * _AUDIO_RATE:
                    progress(min(samples, total_samples) if total_samples else samples,
                             total_samples, "audio_extract", 0)
                    last_report = samples
                if samples - start >= chunk_seconds * _AUDIO_RATE:
                    for packet in track.encode(None):
                        sink.mux(packet)
                    sink.close()
                    sink = None
                    start = samples

            for frame in container.decode(source):
                if resampler is None:
                    # AAC output always uses this sample format; each chunk has
                    # its own encoder, but the resampler is shared across all.
                    resampler = AudioResampler(format="fltp", layout=_AUDIO_LAYOUT, rate=_AUDIO_RATE)
                for resampled in resampler.resample(frame):
                    write_frame(resampled)
            if resampler is not None:
                for resampled in resampler.resample(None):
                    write_frame(resampled)
            if sink is not None:
                for packet in track.encode(None):
                    sink.mux(packet)
                sink.close()
                sink = None
            if progress:
                progress(total_samples or samples, total_samples or samples,
                         "audio_extract", 0)
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
    finally:
        if sink is not None:
            sink.close()
    return parts


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
            from .network import dead_loopback_proxy

            with audio.open("rb") as source, httpx.Client(timeout=600.0, trust_env=not dead_loopback_proxy()) as client:
                response = client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"audio": (audio.name, source, "audio/mp4")},
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


def _upload_direct(url: str, key: str, audio: Path) -> dict:
    """With explicit consent, send this audio part straight to private OSS."""
    import httpx
    from urllib.parse import urlsplit
    from .network import dead_loopback_proxy

    suffix = "/v1/transcribe"
    if not url.rstrip("/").endswith(suffix):
        raise RuntimeError("云端直传地址配置不正确。")
    base = url.rstrip("/")[:-len(suffix)]
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(trust_env=not dead_loopback_proxy()) as client:
            issued = client.post(base + suffix + "/direct/init", headers=headers,
                                 json={"size_bytes": audio.stat().st_size}, timeout=30)
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
                                      headers={"Content-Type": "audio/mp4", "Content-Length": str(audio.stat().st_size)},
                                      timeout=600)
            if uploaded.status_code not in (200, 201):
                raise RuntimeError(f"音频直传到 OSS 失败（HTTP {uploaded.status_code}）；请检查网络后重试。")
            for attempt in (1, 2):
                try:
                    completed = client.post(base + suffix + "/direct/complete", headers=headers,
                                            json={"upload_id": upload_id}, timeout=1900)
                    break
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    time.sleep(_RETRY_DELAY)
            if completed.status_code != 200:
                raise RuntimeError(f"云端直传转写失败（HTTP {completed.status_code}）；已完成片段会在重试时复用。")
            payload = completed.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
                raise RuntimeError("云端直传返回的转写内容无效。")
            return payload
    except httpx.HTTPError as exc:
        raise RuntimeError("音频直传连接中断；请检查网络后重试。") from exc


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
    with tempfile.TemporaryDirectory(prefix="pku-sync-audio-") as tmp:
        if _audio_duration(video) > _CHUNK_SECONDS:
            parts = _extract_audio_parts(video, Path(tmp), _CHUNK_SECONDS, progress=progress)
        else:
            audio = _extract_audio(video, Path(tmp) / "audio.m4a")
            parts = _split_audio(audio, Path(tmp), _CHUNK_SECONDS)
        if progress:
            progress(0, len(parts), "audio_parts", 0)
        uploader = _upload_direct if direct_oss else _upload
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
        parts: list[tuple[Path, float]] = []
        sink = None
        track = None
        samples = 0
        start = 0
        try:
            for frame in source.decode(stream):
                if sink is None:
                    path = directory / f"part-{len(parts):04d}.m4a"
                    sink = av.open(str(path), mode="w")
                    track = sink.add_stream(_AUDIO_CODEC, rate=_AUDIO_RATE)
                    track.layout = _AUDIO_LAYOUT
                    parts.append((path, start / _AUDIO_RATE))
                for packet in track.encode(frame):
                    sink.mux(packet)
                samples += frame.samples
                if samples - start >= chunk_seconds * _AUDIO_RATE:
                    for packet in track.encode(None):
                        sink.mux(packet)
                    sink.close()
                    sink = None
                    start = samples
            if sink is not None:
                for packet in track.encode(None):
                    sink.mux(packet)
                sink.close()
                sink = None
        finally:
            if sink is not None:
                sink.close()
        return parts or [(audio, 0.0)]


def _transcribe_parts(
    parts: list[tuple[Path, float]], url: str, token: str, video: Path, target: Path,
    progress=None, uploader=None,
) -> dict:
    """Run two uploads at a time; retain successful replies for a later retry."""
    stat = video.stat()
    signature = hashlib.sha256(f"{stat.st_size}:{stat.st_mtime_ns}:{_CHUNK_SECONDS}".encode()).hexdigest()[:16]
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
        raise RuntimeError(f"第 {index + 1}/{len(parts)} 段转写失败；已完成片段会在重试时复用：{error}") from error
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


def _clear_part_cache(video: Path, target: Path, count: int) -> None:
    stat = video.stat()
    signature = hashlib.sha256(f"{stat.st_size}:{stat.st_mtime_ns}:{_CHUNK_SECONDS}".encode()).hexdigest()[:16]
    cache = target.parent / ".transcription-parts"
    for index in range(count):
        (cache / f"{signature}-{index:04d}.json").unlink(missing_ok=True)
