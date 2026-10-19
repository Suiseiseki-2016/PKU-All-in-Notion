"""Place representative recording frames beside local and Notion note sections."""
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



_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _visual_json(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _visual_rows(payload, key):
    rows = payload.get(key, []) if isinstance(payload, dict) else payload
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _local_material(job, name):
    """Resolve a local PDF by filename or catalog title, confined to materials."""
    folder = (job.course_dir / "materials").resolve()
    candidates = [folder / name]
    from ..lecture_source_download import local_upload_source_paths
    from .recordings_api import recording_id
    for path in local_upload_source_paths(job, recording_id(job)):
        if name == path.name or name == path.name[33:]:
            candidates.append(path)
    for row in _visual_rows(_visual_json(folder / "index.json", []), "materials"):
        assets = [str(f) for f in (row.get("files") or []) if isinstance(f, str)]
        assets += [str(a.get("filename") or "") for a in (row.get("attachments") or []) if isinstance(a, dict)]
        if name in [row.get("title"), row.get("path"), *assets]:
            candidates += [folder / f for f in assets]
            candidates.append(folder / str(row.get("path") or ""))
    receipt = _visual_json(job.directory / "keyframes/keyframe-matches.json", {})
    for row in _visual_rows(receipt, "matches"):
        if name in (row.get("title"), row.get("filename"), row.get("material_path")):
            candidates.append(job.course_dir / str(row.get("local_path") or ""))
    for path in candidates:
        path = path.resolve()
        if path.is_relative_to(folder) and path.is_file() and path.suffix.lower() == ".pdf":
            return path
    return None


def _slide_cache(job, name, page):
    from ..store import safe_name
    return job.directory / "keyframes/slides-cache" / f"{safe_name(name, 'slide')}_p{page}"


def _saved_visual_windows(job, markdown):
    """Only use saved intervals when their associated note text still matches."""
    from ..knowledge_workflow import saved_sections
    try:
        body, sections = saved_sections(job.directory / "notes.md")
        if body == markdown:
            return sections
    except RuntimeError:
        pass
    from ..transcript_notes import POLICY, _read, report_path, fingerprint
    report = _visual_json(report_path(job.directory / "notes.md"), {})
    if not isinstance(report, dict) or report.get("policy") != POLICY or report.get("note_sha256") != fingerprint(markdown):
        return []
    sections = []
    try:
        for key in report["requests"]:
            row = _read(job.directory / ".notes-parts" / POLICY, key)
            transcript = row["input"]["transcript"]
            sections.append({"markdown": row["response"],
                             "start": min(float(r["start"]) for r in transcript),
                             "end": max(float(r["end"]) for r in transcript)})
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return []
    return sections


def build_note_visual_sections(job, course_id: str, recording_key: str, markdown: str) -> list[dict]:
    """Link locally available pictures to note headings without a score cutoff."""
    from urllib.parse import quote

    folder = job.directory / "keyframes"
    frames = []
    for item in _visual_rows(_visual_json(folder / "index.json", {}), "keyframes"):
        name = item.get("file")
        if (not isinstance(name, str) or Path(name).name != name or "\\" in name
                or Path(name).suffix.lower() not in _IMAGE_SUFFIXES):
            continue
        path = next((d / name for d in (folder, job.directory / "note-frames") if (d / name).is_file()), None)
        if path is None or not path.resolve().is_relative_to(job.directory.resolve()):
            continue
        try:
            stamp = float(item.get("timestamp", 0))
            if not math.isfinite(stamp) or stamp < 0:
                continue
        except (ValueError, TypeError):
            continue
        frames.append({**item, "file": name, "timestamp": stamp, "path": path})
    frames.sort(key=lambda f: f["timestamp"])
    headings = re.findall(r"^#{2,3} +(.+?)\s*$", markdown, re.M)
    # Container headings such as '课堂重点' have no content of their own.
    if re.search(r"^### +", markdown, re.M):
        headings = [h for h in headings if h not in {"课堂重点", "课程笔记", "本讲总结"}]
    if len(headings) > 1:
        headings = [h for h in headings if "复习建议" not in h]
    if not frames or not headings:
        return []
    sections = _saved_visual_windows(job, markdown)
    anchors = _visual_rows(_visual_json(folder / "slide-alignment.json", {}), "anchors")
    matches = _visual_rows(_visual_json(folder / "keyframe-matches.json", {}), "matches")
    materials = None
    base = f"/api/campus/courses/{quote(course_id, safe='')}/recordings/{quote(recording_key, safe='')}"
    visuals = []

    def score(frame):
        if "_preview_score" not in frame:
            try:
                frame["_preview_score"] = _visual_score(frame["path"])
            except (ImportError, OSError, ValueError):
                frame["_preview_score"] = 0
        return frame["_preview_score"]

    for i, heading in enumerate(headings):
        window = None
        timed = re.search(r"\[(\d+):(\d{2})\s*[–—-]\s*(\d+):(\d{2})\]", heading)
        if timed:
            window = (_seconds(timed[1], timed[2]), _seconds(timed[3], timed[4]))
        else:
            for section in sections:
                if heading in re.findall(r"^#{2,3} +(.+?)\s*$", section["markdown"], re.M):
                    window = (section["start"], section["end"])
                    break
        if window:
            inside = [f for f in frames if window[0] <= f["timestamp"] < window[1]]
            if not inside:
                continue
            frame = max(inside, key=score) if len(inside) > 1 else inside[0]
        else:
            # Fewer pictures than topics: attach each once, in source order.
            if len(frames) < len(headings):
                indices = [round(j * (len(headings) - 1) / max(len(frames) - 1, 1)) for j in range(len(frames))]
                if i not in indices:
                    continue
                frame = frames[indices.index(i)]
            else:
                frame = frames[min(len(frames) - 1, i * len(frames) // len(headings))]
        stamp = frame["timestamp"]
        anchor = frame.get("anchor") if isinstance(frame.get("anchor"), dict) else {}
        if not anchor:
            for row in anchors:
                try:
                    start, end = float(row["start"]), float(row["end"])
                    if start <= stamp < end:
                        anchor = row
                        break
                except (KeyError, TypeError, ValueError):
                    continue
        if not anchor and window:
            overlapping = []
            for row in anchors:
                try:
                    overlap = min(window[1], float(row["end"])) - max(window[0], float(row["start"]))
                    if overlap > 0:
                        overlapping.append((overlap, row))
                except (KeyError, TypeError, ValueError):
                    continue
            if overlapping:
                anchor = max(overlapping, key=lambda pair: pair[0])[1]
        title = str(anchor.get("slide_title") or anchor.get("title") or frame.get("slide_title") or frame.get("slide_file") or "")
        page = anchor.get("slide_page") or anchor.get("page") or frame.get("slide_page")
        if not title and matches:
            match = next((r for r in matches if r.get("file") == frame["file"] or r.get("frame_file") == frame["file"]), None)
            if match is None and len(matches) == 1:
                match = matches[0]
            if match:
                title = str(match.get("slide_title") or match.get("title") or match.get("filename") or match.get("material_path") or "")
                pages = match.get("pages") or []
                page = match.get("slide_page") or match.get("page") or (pages[0] if len(pages) == 1 else None)
        if not title:
            if materials is None:
                from .campus_catalog import campus_course
                try:
                    course = campus_course(job.course_dir.parent, course_id)
                    materials = next((r["materials"] for r in course["lessons"] if r["id"] == recording_key), [])
                except (OSError, ValueError, KeyError):
                    materials = []
            if len(materials) == 1:
                title = materials[0].get("title") or ""
        try:
            page = int(page) if page is not None else None
            if page is not None and page < 1:
                page = None
        except (ValueError, TypeError):
            page = None
        slide_url = ""
        if title and page:
            cache = _slide_cache(job, title, page)
            if any(Path(str(cache) + ext).is_file() for ext in (".jpg", ".png")) or _local_material(job, title):
                slide_url = f"{base}/slide-page?file={quote(title, safe='')}&page={page}"
        time = f"{int(stamp) // 60:02d}:{int(stamp) % 60:02d}"
        visuals.append({"heading": heading, "timestamp": stamp, "time": time,
                        "frame_url": f"{base}/frames/{quote(frame['file'], safe='')}",
                        "frame_caption": f"课堂板书/投影画面 · {time}",
                        "board_note": str(frame.get("board_note") or ""),
                        "slide_title": title, "slide_page": page, "slide_url": slide_url,
                        "slide_caption": (f"对应课件：{title} · 第 {page} 页" if page else f"关联课件：{title}") if title else ""})
    return visuals
