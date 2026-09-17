"""Recording download and the 转译 stages: transcript, keyframes, lecture notes.

Downloads run anywhere. Transcription and keyframe extraction need the optional
`media` extra (`faster-whisper`, `opencv-python`) and in practice run on the
Windows host, where a CUDA GPU turns a 90-minute lecture into a few minutes of
work instead of an hour.
"""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

from .auth import cookie_header
from .models import Recording

CHUNK = 1 << 20  # 1 MiB
# resourcese.pku.edu.cn rejects requests without a player-like referer.
PLAYER_REFERER = "https://onlineroomse.pku.edu.cn/"
# HLS segments are independent; fetch several in flight instead of the 1x
# serial stream ffmpeg does. 8 is a safe middle ground for the campus peer.
HLS_WORKERS = 8


@dataclass
class DownloadResult:
    path: Path | None
    downloaded_bytes: int = 0
    resumed: bool = False
    skipped: bool = False
    error: str = ""


def download_recording(
    client: httpx.Client, recording: Recording, target_dir: Path
) -> DownloadResult:
    """Fetch a lecture recording, resuming a partial file when possible."""
    target_dir.mkdir(parents=True, exist_ok=True)

    if recording.mp4_url:
        return _download_mp4(client, recording.mp4_url, target_dir / "video.mp4")
    if recording.m3u8_url:
        target = target_dir / "video.mp4"
        result = _download_hls_segments(client, recording.m3u8_url, target)
        if result.error:
            # Master playlist, unsupported encryption, or segment failure:
            # ffmpeg's own stream copy is the dependable fallback.
            return _download_hls(client, recording.m3u8_url, target)
        return result
    return DownloadResult(path=None, error=recording.unavailable_reason or "no media URL")


def _download_mp4(client: httpx.Client, url: str, target: Path) -> DownloadResult:
    headers = {"Referer": PLAYER_REFERER}
    have = target.stat().st_size if target.exists() else 0

    total = _remote_size(client, url, headers)
    if total and have == total:
        return DownloadResult(path=target, skipped=True)
    if have > (total or 0) > 0:
        # A shrinking source means the lecture was re-published; start over.
        have = 0

    request_headers = dict(headers)
    if have:
        request_headers["Range"] = f"bytes={have}-"

    try:
        with client.stream("GET", url, headers=request_headers, follow_redirects=True) as resp:
            if have and resp.status_code == 200:
                # The server ignored the range, so the response restarts at zero.
                have = 0
            elif resp.status_code not in (200, 206):
                return DownloadResult(path=None, error=f"HTTP {resp.status_code}")

            written = 0
            mode = "ab" if have else "wb"
            with target.open(mode) as handle:
                for chunk in resp.iter_bytes(CHUNK):
                    handle.write(chunk)
                    written += len(chunk)
    except httpx.HTTPError as exc:
        return DownloadResult(path=None, downloaded_bytes=0, error=str(exc))

    return DownloadResult(path=target, downloaded_bytes=written, resumed=bool(have))


def _remote_size(client: httpx.Client, url: str, headers: dict) -> int:
    """Total length of the remote file, or 0 when the server will not say.

    A ranged GET is used rather than HEAD because the media host answers HEAD
    inconsistently.
    """
    try:
        resp = client.get(
            url, headers={**headers, "Range": "bytes=0-0"}, follow_redirects=True
        )
    except httpx.HTTPError:
        return 0

    content_range = resp.headers.get("content-range", "")
    if "/" in content_range:
        tail = content_range.rsplit("/", 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    if resp.status_code == 200:
        length = resp.headers.get("content-length", "")
        return int(length) if length.isdigit() else 0
    return 0


def _download_hls(client: httpx.Client, url: str, target: Path) -> DownloadResult:
    """Remux an HLS playlist with ffmpeg, which cannot share httpx's cookie jar."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return DownloadResult(path=None, error="ffmpeg not installed")
    if target.exists() and target.stat().st_size > 1_000_000:
        return DownloadResult(path=target, skipped=True)

    cookies = cookie_header(client)
    result = subprocess.run(
        [
            ffmpeg,
            "-headers",
            f"Cookie: {cookies}\r\nReferer: {PLAYER_REFERER}\r\n",
            "-i",
            url,
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-y",
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return DownloadResult(path=None, error=result.stderr[-400:].strip())
    return DownloadResult(path=target, downloaded_bytes=target.stat().st_size)


@dataclass(frozen=True)
class HlsSegment:
    """One media segment of a playlist, with the encryption rule in scope."""

    uri: str  # absolute URL
    key_uri: str | None  # AES-128 key URL; None = cleartext segment
    iv: bytes | None  # 16-byte CBC IV; None = media-sequence-derived


def _parse_media_playlist(raw: str, playlist_url: str) -> tuple[list[HlsSegment], bool]:
    """Turn an HLS playlist body into its segment plan.

    Returns (segments, is_master). A master playlist (``#EXT-X-STREAM-INF``)
    lists variants instead of segments; the caller can then fall back to
    ffmpeg's stream copy, which follows variants by itself. Key scope follows
    EXT-X-KEY tags: each tag applies to every later segment until the next.
    Only AES-128 or cleartext is supported; anything else reports no usable
    segments for that media line, which drops the whole download to the
    ffmpeg fallback.
    """
    if "#EXT-X-STREAM-INF" in raw:
        return [], True

    media_sequence = 0
    for line in raw.splitlines():
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1].strip())
            except ValueError:
                media_sequence = 0
            break

    segments: list[HlsSegment] = []
    key_uri: str | None = None
    key_iv: bytes | None = None
    key_method = "NONE"
    pend_key_iv: bytes | None = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY:"):
            # METHOD=AES-128,URI="https://…",IV=0x… — attributes appear in
            # any order, so a small attribute parser keeps this robust.
            attrs: dict[str, str] = {}
            tail = line[len("#EXT-X-KEY:") :]
            for pair in _split_attributes(tail):
                name, _, value = pair.partition("=")
                attrs[name.strip()] = value.strip().strip('"')
            method = attrs.get("METHOD", "NONE").upper()
            if method == "AES-128":
                key_uri = attrs.get("URI")
                key_method = "AES-128"
                pending_iv = attrs.get("IV")
                pend_key_iv = _parse_iv(pending_iv) if pending_iv else None
            else:
                key_uri = None
                key_method = "NONE"
                pend_key_iv = None
            if method not in ("AES-128", "NONE"):
                return [], False
            continue
        if line.startswith("#") or not line:
            continue
        # A bare URI line is one segment under the current key scope.
        seg_iv = None
        if key_method == "AES-128":
            if not key_uri:
                return [], False
            if pend_key_iv is not None:
                if key_iv != pend_key_iv:
                    key_iv = pend_key_iv
                seg_iv = key_iv
            else:
                seg_iv = None  # media-sequence-derived; index set later
        segments.append(
            HlsSegment(
                uri=urljoin(playlist_url, line),
                key_uri=key_uri if key_method == "AES-128" else None,
                iv=seg_iv,
            )
        )
    return segments, False


def _split_attributes(tail: str) -> list[str]:
    """Split EXT-X-KEY attributes on commas that are not inside quotes."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for ch in tail:
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
        elif ch == "," and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _parse_iv(raw: str) -> bytes | None:
    """``0x…`` hex IV into 16 bytes; malformed values disable explicit IVs."""
    if not raw.lower().startswith("0x"):
        return None
    try:
        value = int(raw, 16)
    except ValueError:
        return None
    return value.to_bytes(16, "big")


def _hls_headers(client: httpx.Client) -> dict[str, str]:
    """Stream-host headers: the pku.edu.cn session cookie + player referer."""
    return {"Cookie": cookie_header(client), "Referer": PLAYER_REFERER}


def _hls_key(client: httpx.Client, key_uri: str) -> bytes:
    resp = client.get(key_uri, headers=_hls_headers(client), follow_redirects=True)
    if resp.status_code != 200 or len(resp.content) != 16:
        raise RuntimeError(f"HLS AES-128 key fetch failed (HTTP {resp.status_code})")
    return resp.content


def _decrypt_segment(raw: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(raw) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _fetch_segment(
    client: httpx.Client,
    seg: HlsSegment,
    index: int,
    media_sequence: int,
    target: Path,
) -> Path:
    """Download one segment to ``target``, resuming an existing file.

    Existing files are trusted (the transport's built-in retries already
    guard against truncated writes; a partial file would have been renamed
    away by the interrupted run because we write to ``.tmp`` first).
    """
    if target.exists():
        return target
    resp = client.get(seg.uri, headers=_hls_headers(client), follow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"HLS segment #{index} failed (HTTP {resp.status_code})")
    data = resp.content
    if seg.key_uri is not None:
        iv = seg.iv
        if iv is None:
            iv = (media_sequence + index).to_bytes(16, "big")
        data = _decrypt_segment(data, _hls_key(client, seg.key_uri), iv)
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.rename(target)
    return target


def _download_hls_segments(
    client: httpx.Client, url: str, target: Path, workers: int = HLS_WORKERS
) -> DownloadResult:
    """Download an HLS lecture by fetching all segments concurrently.

    This replaces the plain ffmpeg stream copy (one serial connection, no
    resume): segments download in parallel into a per-target cache directory
    and merge into the mp4 via a local remux. A master playlist, unsupported
    encryption, or any segment failure returns an error so the caller can
    fall back to the ffmpeg path.
    """
    if target.exists() and target.stat().st_size > 1_000_000:
        return DownloadResult(path=target, skipped=True)

    resp = client.get(url, headers=_hls_headers(client), follow_redirects=True)
    if resp.status_code != 200:
        return DownloadResult(path=None, error=f"HLS playlist HTTP {resp.status_code}")
    segments, is_master = _parse_media_playlist(resp.text, str(resp.url))
    if is_master or not segments:
        return DownloadResult(path=None, error="HLS master playlist or no segments")

    cache = target.parent / f".{target.name}.hls"
    cache.mkdir(parents=True, exist_ok=True)

    media_sequence = 0
    for line in resp.text.splitlines():
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1].strip())
            except ValueError:
                media_sequence = 0
            break

    plan = [
        (index, seg, cache / f"seg_{index:05d}.ts")
        for index, seg in enumerate(segments)
    ]
    failures: list[str] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_segment, client, seg, index, media_sequence, path): index
                for index, seg, path in plan
            }
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - collect and retry below
                    failures.append(f"#{index}: {exc}")
    except Exception as exc:  # noqa: BLE001 - include executor-level failures
        failures.append(str(exc))
    if failures:
        return DownloadResult(
            path=None, error=f"HLS segment download failed: {'; '.join(failures[:5])}"
        )

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return DownloadResult(path=None, error="ffmpeg not installed")
    merged = cache / "merged.ts"
    with merged.open("wb") as handle:
        for _index, _seg, path in plan:
            handle.write(path.read_bytes())
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(merged),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return DownloadResult(path=None, error=result.stderr[-400:].strip())
    shutil.rmtree(cache, ignore_errors=True)
    return DownloadResult(path=target, downloaded_bytes=target.stat().st_size)


def transcribe(video: Path, target: Path, settings) -> dict:
    """Speech-to-text with faster-whisper, cached on the transcript's own file."""
    if target.exists():
        return json.loads(target.read_text("utf-8"))

    from faster_whisper import WhisperModel

    model = WhisperModel(
        settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )
    started = time.time()
    segments, info = model.transcribe(
        str(video),
        language="zh",
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    entries = [
        {"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()}
        for seg in segments
    ]
    payload = {
        "video": video.name,
        "language": info.language,
        "duration": entries[-1]["end"] if entries else 0,
        "elapsed": round(time.time() - started, 1),
        "model": settings.whisper_model,
        "device": settings.whisper_device,
        "segments": entries,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    return payload


def extract_keyframes(
    video: Path, out_dir: Path, threshold: float = 30.0, min_gap_sec: float = 3.0
) -> list[dict]:
    """Save a frame each time the projected slide changes.

    Frames are compared once per second at low resolution, so a lecture yields
    roughly one image per slide instead of thousands of near-duplicates.
    """
    index_path = out_dir / "index.json"
    if index_path.exists():
        return json.loads(index_path.read_text("utf-8")).get("keyframes", [])

    import cv2
    import numpy as np

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise RuntimeError(f"cannot open video: {video}")

    out_dir.mkdir(parents=True, exist_ok=True)
    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    sample_interval = max(int(fps), 1)
    min_gap_frames = int(min_gap_sec * fps)

    frames: list[dict] = []
    previous = None
    frame_index = 0
    last_saved = -min_gap_frames

    while True:
        # grab() advances without decoding into a numpy array, so only the one
        # frame per second that is actually compared gets retrieved. Decoding
        # every frame of a two-hour lecture takes half an hour on its own.
        if not capture.grab():
            break
        if frame_index % sample_interval == 0:
            ok, frame = capture.retrieve()
            if not ok:
                frame_index += 1
                continue
            gray = cv2.cvtColor(cv2.resize(frame, (320, 180)), cv2.COLOR_BGR2GRAY)
            if previous is not None:
                diff = float(np.mean(cv2.absdiff(gray, previous)))
                if diff > threshold and (frame_index - last_saved) >= min_gap_frames:
                    seconds = frame_index / fps
                    name = f"frame_{len(frames):04d}_{_stamp(seconds, compact=True)}.jpg"
                    if _write_jpeg(cv2, out_dir / name, frame):
                        frames.append(
                            {
                                "index": len(frames),
                                "timestamp": round(seconds, 2),
                                "time": _stamp(seconds),
                                "file": name,
                                "diff": round(diff, 2),
                            }
                        )
                        last_saved = frame_index
            previous = gray
        frame_index += 1

    total_frames = frame_index
    capture.release()
    index_path.write_text(
        json.dumps(
            {
                "video": video.name,
                "fps": fps,
                "duration": round(total_frames / fps, 2) if fps else 0,
                "keyframes": frames,
            },
            ensure_ascii=False,
            indent=1,
        ),
        "utf-8",
    )
    return frames


def _write_jpeg(cv2, target: Path, frame) -> bool:
    """Encode in memory and write with Python's file API.

    cv2.imwrite goes through a narrow-char path on Windows and silently fails
    for the Chinese course directories this pipeline writes into.
    """
    ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return False
    target.write_bytes(buffer.tobytes())
    return True


def write_notes(
    transcript: dict,
    keyframes: list[dict],
    target: Path,
    settings,
    title: str,
    window_seconds: int = 480,
) -> Path | None:
    """Turn a transcript into lecture notes, one LLM call per time window.

    Windowing keeps each request small enough to stay coherent and lets a failed
    window degrade into its raw transcript instead of losing the whole lecture.
    """
    if target.exists():
        return target
    if not settings.openai_api_key:
        return None

    segments = transcript.get("segments") or []
    if not segments:
        return None

    from openai import OpenAI

    llm = OpenAI(base_url=settings.openai_base_url, api_key=settings.openai_api_key)

    sections = [f"# {title}", ""]
    for window in _windows(segments, window_seconds):
        span = f"{_stamp(window['start'])}–{_stamp(window['end'])}"
        slides = [k["time"] for k in keyframes if window["start"] <= k["timestamp"] < window["end"]]
        sections += [f"## {span}", ""]
        if slides:
            sections += [f"*幻灯片切换: {', '.join(slides)}*", ""]

        try:
            sections += [_summarize(llm, settings.notes_model, title, span, window["text"]), ""]
        except Exception as exc:
            sections += [f"> 生成失败（{exc}），以下为原始转录：", "", window["text"], ""]

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(sections), "utf-8")
    return target


def _summarize(llm, model: str, title: str, span: str, text: str) -> str:
    response = llm.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是一名认真的大学生助教，正在把课堂录音的自动转录整理成课堂笔记。"
                    "转录可能有识别错误，请根据上下文推断出正确的术语。"
                    "只输出 Markdown 正文，不要重复标题，不要写客套话。"
                    "结构为：要点列表、关键概念解释、以及需要课后确认的疑问（如有）。"
                    "以下三类必须逐条保留、引用老师原话并加粗标注，"
                    "绝不能为了压缩篇幅而省略或改写（压缩只能作用于普通讲解内容）："
                    "① 考核相关（如「要考」「会考」「必考」「小测可能会考」"
                    "「期末会问」「考试经常遇到」「这是重点」）；"
                    "② 作业与考核安排（截止日期与时间、占比、提交平台与格式、判分方式、逾期规则）；"
                    "③ 老师口头布置的任务（如「下周小测」「这个练习下去做」「下节课要交」"
                    "「我后面会考大家」），含具体日期时间的一律照抄。"
                    "另外，老师在课上演示或让大家写的代码、命令、报错信息，"
                    "必须用代码块原样保留，不要改写成文字描述。"
                ),
            },
            {
                "role": "user",
                "content": f"课程：{title}\n时间段：{span}\n\n转录内容：\n{text}",
            },
        ],
        temperature=0.2,
    )
    return (response.choices[0].message.content or "").strip()


def _windows(segments: list[dict], window_seconds: int) -> list[dict]:
    windows: list[dict] = []
    current: dict | None = None

    for segment in segments:
        if current is None or segment["start"] - current["start"] >= window_seconds:
            current = {"start": segment["start"], "end": segment["end"], "parts": []}
            windows.append(current)
        current["end"] = segment["end"]
        current["parts"].append(segment["text"])

    for window in windows:
        window["text"] = " ".join(part for part in window.pop("parts") if part)
    return [w for w in windows if w["text"]]


def _stamp(seconds: float, compact: bool = False) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}m{secs:02d}s" if compact else f"{minutes:02d}:{secs:02d}"
