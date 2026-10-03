"""Local, evidence-preserving review of an ambiguous spoken term."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..pipeline import RecordingJob
from ..note_quality import ambiguous_public_key_segment

_RAC = re.compile(r"(?<![A-Za-z])rac\s*算法", re.I)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(job: RecordingJob) -> tuple[bytes, dict]:
    raw = (job.directory / "transcript.json").read_bytes()
    transcript = json.loads(raw)
    if not isinstance(transcript, dict) or not isinstance(transcript.get("segments"), list):
        raise ValueError("本机转写格式无法校对。")
    return raw, transcript


def review_marker(job: RecordingJob) -> dict:
    try:
        marker = json.loads((job.directory / "transcript-review.json").read_text("utf-8"))
        raw, _ = _read(job)
    except (OSError, ValueError, TypeError):
        return {}
    return marker if isinstance(marker, dict) and marker.get("corrected_sha256") == _digest(raw) else {}


def review_pending(job: RecordingJob) -> bool:
    return bool(review_marker(job).get("needs_regeneration"))


def review_status(job: RecordingJob, key: str) -> dict:
    try:
        raw, transcript = _read(job)
    except (OSError, ValueError, TypeError):
        return {"status": "none", "transcript_available": False, "pending_regeneration": False}
    marker = review_marker(job)
    result = {
        "status": "none", "transcript_available": True,
        "transcript_sha256": _digest(raw), "video_available": job.video.is_file(),
        "pending_regeneration": bool(marker.get("needs_regeneration")),
    }
    match = ambiguous_public_key_segment(transcript["segments"])
    if match:
        index, segment = match
        spoken = str(segment.get("text") or "")
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start)
        def stamp(seconds: float) -> str:
            return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"
        result["status"] = "needs_review"
        result["target"] = {
            "segment_index": index, "start": start, "end": end,
            "start_label": stamp(start), "end_label": stamp(end),
            "excerpt": spoken[:500], "video_url": f"/api/recordings/{key}/preview",
        }
    return result


def confirm_rsa(job: RecordingJob, *, segment_index: int, transcript_sha256: str,
                confirmed_spoken_term: str, confirmed: bool) -> dict:
    if not confirmed or confirmed_spoken_term != "RSA":
        raise ValueError("请先听原音，并确认老师明确说的是 RSA。")
    if not job.video.is_file():
        raise ValueError("本机缺少录像原音，无法据此校正转写。请先下载录像。")
    raw, transcript = _read(job)
    digest = _digest(raw)
    if transcript_sha256 != digest:
        raise ValueError("转写已变化，请刷新后重新核对原音。")
    segments = transcript["segments"]
    if segment_index < 0 or segment_index >= len(segments):
        raise ValueError("这段转写已变化，请刷新。")
    segment = segments[segment_index]
    match = ambiguous_public_key_segment(segments)
    if not isinstance(segment, dict) or not _RAC.search(str(segment.get("text") or "")) \
            or match is None or match[0] != segment_index:
        raise ValueError("这段转写已变化，请刷新。")
    original_text = str(segment["text"])
    segment["text"] = _RAC.sub("RSA算法", original_text, count=1)
    corrected = json.dumps(transcript, ensure_ascii=False, indent=2).encode("utf-8")
    path = job.directory / "transcript.json"
    backup = job.directory / f"transcript.before-review-{digest[:12]}.json"
    if not backup.exists():
        backup.write_bytes(raw)
    temporary = path.with_name("transcript.review.tmp")
    temporary.write_bytes(corrected)
    temporary.replace(path)
    marker = {
        "original_sha256": digest, "corrected_sha256": _digest(corrected),
        "segment_index": segment_index, "start": segment.get("start"), "end": segment.get("end"),
        "original_text": original_text, "corrected_text": segment["text"],
        "confirmed_spoken_term": "RSA", "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "needs_regeneration": (job.directory / "notes.md").is_file(),
    }
    marker_path = job.directory / "transcript-review.json"
    marker_temp = marker_path.with_name("transcript-review.tmp")
    try:
        marker_temp.write_text(json.dumps(marker, ensure_ascii=False, indent=2), "utf-8")
        marker_temp.replace(marker_path)
    except OSError:
        path.write_bytes(raw)
        raise
    return {"status": "corrected", "pending_regeneration": marker["needs_regeneration"]}


def clear_regeneration_pending(job: RecordingJob) -> None:
    marker = review_marker(job)
    if not marker or not marker.get("needs_regeneration"):
        return
    marker["needs_regeneration"] = False
    path = job.directory / "transcript-review.json"
    temporary = path.with_name("transcript-review.tmp")
    temporary.write_text(json.dumps(marker, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)
