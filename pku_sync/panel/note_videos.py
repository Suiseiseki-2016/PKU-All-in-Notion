"""Publish short, re-listenable recording clips beside a Notion note."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..notion import NotionClient, _video_block

_MARKER = re.compile(r"（回看 (?P<minutes>\d{1,3}):(?P<seconds>\d{2})）")
_CLIP_PAD_BEFORE = 8
_CLIP_DURATION = 20
_MIN_CLIP_SECONDS = 2  # shorter means the marker is outside the recording
_MAX_TOTAL_CLIP_BYTES = 5 * 1024 * 1024
_REPORT_NAME = "notion-video-publication.json"
_CLIP_VERSION = "v4"
# A clip is quoted evidence, so it must show the moment its sentence describes.
# Measured on a real lecture: anchors that point at the wrong minute score 0.00
# against the audio there, while correct ones score 0.33-0.67.
_CORROBORATION_SPAN = 20
_MIN_CORROBORATION = 0.15


class VideoClipError(RuntimeError):
    """A local clip could not be produced without affecting the note."""


def _plain(block: dict) -> str:
    kind = block.get("type") or ""
    body = block.get(kind) or {}
    return "".join(
        part.get("plain_text") or part.get("text", {}).get("content", "")
        for part in body.get("rich_text") or []
    )


def _caption(block: dict) -> str:
    kind = block.get("type") or ""
    return "".join(
        part.get("plain_text") or part.get("text", {}).get("content", "")
        for part in (block.get(kind) or {}).get("caption") or []
    )


def _seconds(minutes: str, seconds: str) -> int:
    return int(minutes) * 60 + int(seconds)


def _stamps(notes: str) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    seen: set[int] = set()
    for match in _MARKER.finditer(notes):
        seconds = _seconds(match["minutes"], match["seconds"])
        if seconds in seen:
            continue
        seen.add(seconds)
        found.append((f"{int(match['minutes']):02d}:{int(match['seconds']):02d}", seconds))
    return found


def needs_video_clips(notes_path: Path) -> bool:
    try:
        return bool(_stamps(notes_path.read_text("utf-8")))
    except OSError:
        return False


def _grams(text: str) -> set[str]:
    from ..media import to_simplified

    body = to_simplified(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or "")))
    return {body[index:index + 2] for index in range(len(body) - 1)}


def _claims_by_timestamp(notes: str) -> dict[int, list[str]]:
    """The note sentence each marker annotates, keyed by its timestamp."""
    claims: dict[int, list[str]] = {}
    for match in _MARKER.finditer(notes):
        seconds = _seconds(match["minutes"], match["seconds"])
        line_start = notes.rfind("\n", 0, match.start()) + 1
        body = _MARKER.sub("", notes[line_start:match.start()])
        parts = [part for part in re.split(r"[。；]", body) if part.strip()]
        claims.setdefault(seconds, []).append(parts[-1] if parts else body)
    return claims


def _load_segments(directory: Path) -> list[dict] | None:
    try:
        data = json.loads((directory / "transcript.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    segments = data.get("segments")
    return segments if isinstance(segments, list) else None


def corroboration(claims: list[str], segments: list[dict], timestamp: int) -> float:
    """How much of the note's wording is actually spoken around ``timestamp``.

    Anchors are chosen by text similarity when the note is written, so a claim
    can carry a plausible but wrong timestamp. Publishing a clip turns that
    into quoted evidence of a moment the speaker never said, which is worse
    than the text marker alone.
    """
    spoken = _grams(" ".join(
        str(segment.get("text") or "") for segment in segments
        if isinstance(segment, dict)
        and isinstance(segment.get("start"), (int, float))
        and abs(float(segment["start"]) - timestamp) <= _CORROBORATION_SPAN
    ))
    if not spoken:
        return 0.0
    best = 0.0
    for claim in claims:
        wanted = _grams(claim)
        if wanted:
            best = max(best, len(wanted & spoken) / len(wanted))
    return best


def _source_signature(video: Path) -> str:
    stat = video.stat()
    return hashlib.sha256(
        f"{_CLIP_VERSION}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    ).hexdigest()[:16]


def _clip_path(video: Path, directory: Path, timestamp: int) -> Path:
    source_key = _source_signature(video)
    name = f"{video.stem}-{source_key}-{timestamp:06d}.mp4"
    return directory / name


def _extract_clip(video: Path, target: Path, start: int, duration: int) -> None:
    """Stream-copy a clip in-process with the bundled FFmpeg libraries (PyAV).

    The clip starts at the keyframe at or before ``start`` (like
    ``ffmpeg -ss ... -c copy``), so a marker is never missed, and both
    streams keep their original encoding.
    """
    try:
        import av
    except ImportError as exc:  # av is a hard dependency of the product.
        raise VideoClipError(
            "安装包缺少多媒体组件，无法生成录像片段；文字笔记仍可发布，请按时间标记回看原录像。"
        ) from exc
    seek_seconds = max(0, start)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp.mp4")
    temporary.unlink(missing_ok=True)
    muxed = 0
    last_video_reference: int | None = None
    try:
        with av.open(str(video)) as source:
            if not source.streams.video:
                raise VideoClipError("录像文件没有视频轨。")
            video_stream = source.streams.video[0]
            audio_stream = source.streams.audio[0] if source.streams.audio else None
            streams = [video_stream] + ([audio_stream] if audio_stream else [])
            source.seek(
                int(seek_seconds / video_stream.time_base),
                stream=video_stream,
                backward=True,
            )
            end_seconds = seek_seconds + duration
            with av.open(
                str(temporary), "w", format="mp4",
                options={"movflags": "+faststart"},
            ) as clip:
                out_streams = {
                    s.index: clip.add_stream_from_template(s) for s in streams
                }
                start_refs: dict[int, int] = {}
                for packet in source.demux(streams):
                    if packet.dts is None:
                        continue
                    index = packet.stream.index
                    if index not in out_streams:
                        continue
                    reference = packet.pts if packet.pts is not None else packet.dts
                    if index not in start_refs:
                        start_refs[index] = reference
                    if float(reference * packet.stream.time_base) > end_seconds + 0.5:
                        if packet.stream.type == "video":
                            break
                        continue
                    if packet.pts is not None:
                        packet.pts -= start_refs[index]
                    packet.dts -= start_refs[index]
                    if packet.stream.type == "video":
                        last_video_reference = reference
                    packet.stream = out_streams[index]
                    clip.mux(packet)
                    muxed += 1
    except VideoClipError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise VideoClipError(f"生成录像片段失败：{str(exc)[:160]}") from exc
    video_index = video_stream.index
    span = None
    if last_video_reference is not None and video_index in start_refs:
        span = float(
            (last_video_reference - start_refs[video_index]) * video_stream.time_base
        )
    if (muxed == 0 or span is None or span < _MIN_CLIP_SECONDS
            or not temporary.is_file() or temporary.stat().st_size == 0):
        temporary.unlink(missing_ok=True)
        raise VideoClipError("标记位置可能超出录像范围，没有产生片段。")
    temporary.replace(target)


def clip_for_timestamp(
    video: Path, directory: Path, timestamp: int, max_bytes: int | None = None
) -> Path:
    """Return a cached clip that fits the caller's remaining byte budget."""
    budget = _MAX_TOTAL_CLIP_BYTES if max_bytes is None else max(0, int(max_bytes))
    return _clip_for_timestamp(video, directory, timestamp, budget)


def _clip_for_timestamp(
    video: Path, directory: Path, timestamp: int, max_bytes: int
) -> Path:
    if not video.is_file():
        raise VideoClipError("本机没有原始录像。")
    target = _clip_path(video, directory, timestamp)
    if target.is_file() and 0 < target.stat().st_size <= max_bytes:
        return target
    if target.is_file():
        raise VideoClipError(
            f"该片段需要 {target.stat().st_size} 字节，剩余额度只有 {max_bytes} 字节。"
        )
    _extract_clip(video, target, timestamp - _CLIP_PAD_BEFORE, _CLIP_DURATION)
    clip_bytes = target.stat().st_size
    if clip_bytes > max_bytes:
        target.unlink(missing_ok=True)
        raise VideoClipError(
            f"该片段需要 {clip_bytes} 字节，超过本节剩余额度 {max_bytes} 字节。"
        )
    return target


def _write_report(directory: Path, report: dict) -> None:
    path = directory / _REPORT_NAME
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    temporary.replace(path)


def publish_note_videos(notion: NotionClient, note_id: str, directory: Path) -> int:
    """Add one idempotent video block per re-listen marker.

    Clip failures are recorded locally and deliberately do not roll back the
    text note. A later publish retries only the missing clips.
    """
    video = directory / "video.mp4"
    notes_path = directory / "notes.md"
    report = {
        "version": _CLIP_VERSION,
        "source_video": video.name,
        "source_signature": "",
        "budget_bytes": _MAX_TOTAL_CLIP_BYTES,
        "used_bytes": 0,
        "complete": False,
        "clips": [],
    }
    if not video.is_file():
        report.update({"status": "unavailable", "reason": "原始录像不在本机"})
        _write_report(directory, report)
        return 0
    try:
        notes = notes_path.read_text("utf-8")
    except OSError as exc:
        report.update({"status": "unavailable", "reason": "笔记文件不可读取"})
        _write_report(directory, report)
        return 0
    markers = _stamps(notes)
    if not markers:
        report.update({"status": "not_needed", "complete": True})
        _write_report(directory, report)
        return 0
    source_signature = _source_signature(video)
    report["source_signature"] = source_signature
    previous: dict = {}
    try:
        previous = json.loads((directory / _REPORT_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        pass
    if (previous.get("version") == _CLIP_VERSION
            and previous.get("source_signature") == source_signature):
        try:
            report["used_bytes"] = max(0, int(previous.get("used_bytes") or 0))
        except (TypeError, ValueError):
            report["used_bytes"] = 0

    segments = _load_segments(directory)
    claims = _claims_by_timestamp(notes)
    report["corroborated"] = segments is not None

    blocks = notion.list_children(note_id)
    existing = {
        _caption(block)
        for block in blocks if block.get("type") == "video"
    }
    inserted = 0
    failures = 0
    budget_skips = 0
    unverified = 0
    verification_pending = 0
    used_bytes = report["used_bytes"]
    clips_dir = directory / "video-clips"
    for stamp, timestamp in markers:
        caption = f"课堂录像 {stamp} · 回看原音"
        item = {"timestamp": stamp, "caption": caption}
        if caption in existing:
            item["status"] = "existing"
            report["clips"].append(item)
            continue
        if segments is None:
            item.update({"status": "unverified",
                         "reason": "没有转写文件，无法核对标记指向的内容"})
            unverified += 1
            verification_pending += 1
            report["clips"].append(item)
            continue
        score = corroboration(claims.get(timestamp) or [], segments, timestamp)
        if score < _MIN_CORROBORATION:
            item.update({"status": "unverified", "score": round(score, 3),
                         "reason": "该时间点的原话与笔记内容对不上，只保留文字标记"})
            unverified += 1
            report["clips"].append(item)
            continue
        item["score"] = round(score, 3)
        try:
            remaining = _MAX_TOTAL_CLIP_BYTES - used_bytes
            if remaining <= 0:
                item.update({"status": "budget_exceeded", "remaining_bytes": 0})
                budget_skips += 1
                report["clips"].append(item)
                continue
            clip = clip_for_timestamp(video, clips_dir, timestamp, remaining)
            clip_bytes = clip.stat().st_size
            reference = notion.upload_file(clip)
            block = _video_block(caption, reference)
            after = next((block_row.get("id") for block_row in blocks
                          if stamp in _plain(block_row)), None)
            notion.append_blocks(note_id, [block], after=after, retry=False)
            existing.add(caption)
            used_bytes += clip_bytes
            item.update({"status": "inserted", "file": str(clip.relative_to(directory)),
                         "bytes": clip_bytes})
            inserted += 1
        except Exception as exc:
            item.update({"status": "failed", "error": str(exc)[:200]})
            failures += 1
        report["clips"].append(item)
    # A low-scoring marker is a final decision, not a retryable failure, so it
    # does not pin the recording to disk. Missing transcripts are different:
    # verification has not happened yet, so keep the source for a later retry.
    pending = failures or budget_skips or verification_pending
    report.update({
        "status": "complete" if not pending else "partial",
        "complete": not pending,
        "inserted": inserted,
        "failed": failures,
        "skipped_budget": budget_skips,
        "skipped_unverified": unverified,
        "verification_pending": verification_pending,
        "used_bytes": used_bytes,
    })
    _write_report(directory, report)
    return inserted


def video_publication_complete(directory: Path) -> bool:
    """Whether it is safe to remove the original after a successful publish."""
    try:
        report = json.loads((directory / _REPORT_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return bool(report.get("complete") is True)
