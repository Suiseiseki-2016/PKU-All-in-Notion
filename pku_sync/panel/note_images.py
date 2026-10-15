"""Place representative recording frames beside the matching Notion note sections."""
from __future__ import annotations

import json
import hashlib
import math
import re
import uuid
from pathlib import Path

from ..notion import NotionClient, markdown_to_blocks

_WINDOW = re.compile(r"^(\d+):(\d{2})[–-](\d+):(\d{2})$")
_CAPTION_PREFIX = "课堂画面 "

class NoteFrameError(RuntimeError):
    """The original video could not supply a missing note-window picture."""

def _text(block: dict) -> str:
    kind = block.get("type") or ""
    return "".join(
        part.get("plain_text") or part.get("text", {}).get("content", "")
        for part in (block.get(kind) or {}).get("rich_text") or []
    )

def _seconds(minutes: str, seconds: str) -> int:
    return int(minutes) * 60 + int(seconds)

def _visual_score(path: Path) -> float:
    """Prefer legible slide views over blank screens and distant classroom shots."""
    import cv2
    import numpy as np

    frame = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if frame is None:
        return 0.0
    gray = cv2.resize(frame, (320, 180))
    bright = float(np.mean(gray > 185))
    edges = float(np.mean(cv2.Canny(gray, 50, 150) > 0))
    contrast = float(np.std(gray)) / 255
    return 2 * bright + 6 * edges + contrast

def _frames(directory: Path) -> list[dict]:
    folder = directory / "keyframes"
    try:
        index = json.loads((folder / "index.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    rows = []
    for item in index.get("keyframes", []):
        if not isinstance(item, dict):
            continue
        name = item.get("file")
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".jpg"):
            continue
        path = folder / name
        if not path.is_file():
            continue
        try:
            stamp = float(item["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        score = _visual_score(path)
        if score < 1.6:
            continue
        rows.append({"path": path, "time": str(item.get("time") or ""),
                     "timestamp": stamp, "score": score})
    return sorted(rows, key=lambda row: row["timestamp"])

def _choose(frames: list[dict], start: int, end: int) -> dict | None:
    midpoint = (start + end) / 2
    inside = [row for row in frames if start <= row["timestamp"] < end]
    if inside:
        return max(inside, key=lambda row: row["score"] -
                   abs(row["timestamp"] - midpoint) / max(end - start, 1) * 0.25)
    before = [row for row in frames if row["timestamp"] < start and start - row["timestamp"] <= 300]
    return before[-1] if before else None

def _sample_window(video: Path, folder: Path, start: float, end: float) -> list[dict]:
    """Seek original frames for a window missed by slide-change extraction.

    The supplemental cache is separate from the original keyframe index.
    Repeated publication reuses the same video/time files without decoding.
    """
    import cv2
    from ..media import _stamp, _write_jpeg

    stat = video.stat()
    signature = hashlib.sha256(
        f"note-window-v1:{video.name}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    ).hexdigest()[:20]
    stamps = sorted({round(start + (end - start) * fraction, 3)
                     for fraction in (.25, .5, .75)})
    rows = []
    capture = None
    try:
        for stamp in stamps:
            path = folder / f"window-{signature}-{round(stamp * 1000):09d}.jpg"
            if not path.is_file():
                if capture is None:
                    capture = cv2.VideoCapture(str(video))
                    if not capture.isOpened():
                        raise NoteFrameError("无法打开原录像以补充课堂画面；文字笔记已保留。")
                fps = capture.get(cv2.CAP_PROP_FPS)
                if not math.isfinite(fps) or fps <= 0:
                    raise NoteFrameError("原录像帧率无法读取；文字笔记已保留。")
                if not capture.set(cv2.CAP_PROP_POS_FRAMES, round(stamp * fps)):
                    raise NoteFrameError("原录像无法定位到本段时间；文字笔记已保留。")
                ok, frame = capture.read()
                actual = (capture.get(cv2.CAP_PROP_POS_FRAMES) - 1) / fps
                if (not ok or not math.isfinite(actual)
                        or abs(actual - stamp) > max(.1, 1 / fps)):
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                staged = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
                try:
                    if not _write_jpeg(cv2, staged, frame):
                        raise NoteFrameError("课堂画面保存失败；文字笔记已保留，请重试。")
                    staged.replace(path)
                finally:
                    staged.unlink(missing_ok=True)
            score = _visual_score(path)
            if score >= 1.6:
                rows.append({"path": path, "timestamp": stamp,
                             "time": _stamp(stamp), "score": score})
    finally:
        if capture is not None:
            capture.release()
    return rows

def _window_frame(frames, start, end, directory):
    """Only a saved original note window may trigger local supplemental frames."""
    if directory is not None and not any(start <= row["timestamp"] < end for row in frames):
        video = directory / "video.mp4"
        if video.is_file():
            try:
                sampled = _sample_window(video, directory / "note-frames", start, end)
            except OSError as exc:
                raise NoteFrameError("课堂画面保存失败；文字笔记已保留，请重试。") from exc
            return _choose(sampled, start, end)
    return _choose(frames, start, end)

def _organized_placements(blocks, frames, notes_path, body_offset, directory=None):
    """Use saved original packet times and block positions, not prose matching."""
    from ..transcript_notes import POLICY, _read, _body, fingerprint, report_path

    try:
        if type(body_offset) is not int or body_offset < 0:
            return None
        report = json.loads(report_path(notes_path).read_text("utf-8"))
        if report.get("policy") != POLICY or report.get("status") != "organized":
            return None
        body = notes_path.read_text("utf-8")
        if report["note_sha256"] != fingerprint(body):
            return None
        cache = notes_path.parent / ".notes-parts" / POLICY
        records = [_read(cache, key) for key in report["requests"]]
        if not records or any(row is None for row in records):
            return None
        responses = [row["response"] for row in records]
        if _body(report["title"], responses) != body:
            return None
        prefix_count = len(markdown_to_blocks(f"# {report['title']}\n\n"))
        counts = [len(markdown_to_blocks(text)) for text in responses]
        body_count = len(markdown_to_blocks(body))
        if prefix_count + sum(counts) != body_count:
            # A literal unfinished Markdown fence may span responses. Keep
            # the older picture gallery rather than guessing its placement.
            return None
        plain = [block for block in blocks if block.get("type") not in {"image", "video"}]
        if len(plain) < body_offset + body_count:
            return None
        # Publication's own source notices precede the exact submitted body.
        # Existing pictures do not affect these ordered text-block offsets.
        offset = body_offset + prefix_count
        placements = []
        for record, count in zip(records, counts):
            rows = record["input"]["transcript"]
            start = min(float(row["start"]) for row in rows)
            end = max(float(row["end"]) for row in rows)
            frame = _window_frame(frames, start, end, directory)
            if frame is not None and count:
                placements.append((plain[offset]["id"], frame))
            offset += count
        return placements
    except NoteFrameError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
        return None

def _topic_placements(blocks, frames, notes_path, body_offset, directory=None):
    """Optional heading time labels locate pictures; prose is never graded.

    Missing labels leave the older gallery available. Only the submitted body
    is considered, so a student's appended heading cannot acquire an image.
    """
    from ..transcript_notes import TOPIC_POLICY, _topic_read, _body, fingerprint, report_path

    try:
        if type(body_offset) is not int or body_offset < 0:
            return None
        report = json.loads(report_path(notes_path).read_text("utf-8"))
        if report.get("policy") != TOPIC_POLICY or report.get("status") != "organized":
            return None
        body = notes_path.read_text("utf-8")
        cache = notes_path.parent / ".notes-parts" / TOPIC_POLICY
        merged = _topic_read(cache, report["merge_request"])
        if (merged is None or merged["stage"] != "merge" or report["note_sha256"] != fingerprint(body)
                or _body(report["title"], [merged["response"]]) != body):
            return None
        rows = merged["input"]["original_transcript"]
        lower = min(float(row["start"]) for row in rows)
        upper = max(float(row["end"]) for row in rows)
        plain = [b for b in blocks if b.get("type") not in {"image", "video"}]
        count = len(markdown_to_blocks(body))
        if len(plain) < body_offset + count:
            return None
        placements = []
        timed_windows = 0
        for block in plain[body_offset:body_offset + count]:
            if block.get("type") not in {"heading_1", "heading_2", "heading_3"}:
                continue
            label = _text(block).rsplit("[", 1)[-1]
            if not label.endswith("]"):
                continue
            match = _WINDOW.fullmatch(label[:-1])
            if not match:
                continue
            start, end = _seconds(match[1], match[2]), _seconds(match[3], match[4])
            if not lower <= start < end <= upper + 1:
                continue
            timed_windows += 1
            frame = _window_frame(frames, start, end, directory)
            if frame is not None:
                placements.append((block["id"], frame))
        return placements if timed_windows else None
    except NoteFrameError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
        return None

def _knowledge_placements(blocks, frames, notes_path, body_offset, directory=None):
    """Use verified source intervals and exact body offsets, not model labels."""
    from ..knowledge_workflow import POLICIES, saved_sections
    from ..note_editor import report_path
    try:
        if type(body_offset) is not int or body_offset < 0:
            return None
        report = json.loads(report_path(notes_path).read_text("utf-8"))
        if report.get("policy") not in POLICIES:
            return None
        body, sections = saved_sections(notes_path)
        if notes_path.read_text("utf-8") != body:
            return None
        prefix = len(markdown_to_blocks(f"# {report['title']}\n\n"))
        def text_count(markdown):
            return sum(b.get("type") not in {"image", "video"} for b in markdown_to_blocks(markdown))
        counts = [text_count(s["markdown"]) for s in sections]
        if prefix + sum(counts) != text_count(body):
            return None
        plain = [b for b in blocks if b.get("type") not in {"image", "video"}]
        if len(plain) < body_offset + prefix + sum(counts):
            return None
        offset, placements = body_offset + prefix, []
        for section, count in zip(sections, counts):
            inside = any(section["start"] <= f["timestamp"] < section["end"] for f in frames)
            available_video = directory is not None and (directory / "video.mp4").is_file()
            frame = (_window_frame(frames, section["start"], section["end"], directory)
                     if inside or available_video else None)
            if frame is not None and count:
                placements.append((plain[offset]["id"], frame))
            offset += count
        return placements
    except NoteFrameError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
        return None

def publish_note_images(notion: NotionClient, note_id: str, directory: Path, *, notes_path: Path | None = None,
                        body_offset: int | None = None) -> int:
    """Insert one slide per time window; retries skip captions already present."""
    frames = _frames(directory)
    if not frames and not (directory / "video.mp4").is_file():
        return 0
    blocks = notion.list_children(note_id)
    existing = {
        "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                for part in (block.get("image") or {}).get("caption") or [])
        for block in blocks if block.get("type") == "image"
    }
    placements = _knowledge_placements(blocks, frames, notes_path or directory / "notes.md", body_offset, directory)
    if placements is None:
        placements = _organized_placements(blocks, frames, notes_path or directory / "notes.md", body_offset, directory)
    if placements is None:
        placements = _topic_placements(blocks, frames, notes_path or directory / "notes.md", body_offset, directory)
    organized = placements is not None
    placements = placements or []
    for block in ([] if organized else blocks):
        if block.get("type") != "heading_2":
            continue
        match = _WINDOW.fullmatch(_text(block))
        if not match:
            continue
        start = _seconds(match[1], match[2])
        end = _seconds(match[3], match[4])
        frame = _choose(frames, start, end)
        if frame:
            placements.append((block["id"], frame))
    if not placements and not organized:
        # Notes imported from an earlier writer may lack timed headings.
        placements = [(None, frame) for frame in frames[::max(1, len(frames) // 6)][:6]]
    inserted = 0
    for after, frame in placements:
        caption = f"{_CAPTION_PREFIX}{frame['time']} · 来自课程录像"
        if caption in existing:
            continue
        reference = notion.upload_file(frame["path"])
        image = markdown_to_blocks(f"![{caption}]({reference})")[0]
        notion.append_blocks(note_id, [image], after=after, retry=False)
        existing.add(caption)
        inserted += 1
    return inserted

