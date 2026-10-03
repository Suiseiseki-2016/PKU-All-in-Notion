"""Place representative recording frames beside the matching Notion note sections."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..notion import NotionClient, markdown_to_blocks

_WINDOW = re.compile(r"^(\d+):(\d{2})[–-](\d+):(\d{2})$")
_CAPTION_PREFIX = "课堂画面 "


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


def publish_note_images(notion: NotionClient, note_id: str, directory: Path) -> int:
    """Insert one slide per time window; retries skip captions already present."""
    frames = _frames(directory)
    if not frames:
        return 0
    blocks = notion.list_children(note_id)
    existing = {
        "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                for part in (block.get("image") or {}).get("caption") or [])
        for block in blocks if block.get("type") == "image"
    }
    placements = []
    for block in blocks:
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
    if not placements:
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

