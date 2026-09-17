"""Record of what has already been written, so a daily run stays incremental.

Blackboard rewrites a file's `bbcswebdav` id whenever an instructor replaces it,
so the id doubles as a version marker: a stored entry whose source URL still
matches means the local copy is current and can be skipped.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path.exists():
            try:
                self.entries = json.loads(path.read_text("utf-8")).get("entries", {})
            except (json.JSONDecodeError, OSError):
                # A truncated manifest costs one redundant sync, not correctness.
                self.entries = {}

    def is_current(self, rel_path: str, source_url: str, root: Path) -> bool:
        entry = self.entries.get(rel_path)
        if not entry or entry.get("source_url") != source_url:
            return False
        target = root / rel_path
        if not target.exists():
            return False
        return entry.get("size") in (None, target.stat().st_size)

    def record(self, rel_path: str, source_url: str, data: bytes) -> None:
        self.entries[rel_path] = {
            "source_url": source_url,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def forget(self, rel_path: str) -> None:
        self.entries.pop(rel_path, None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entries": dict(sorted(self.entries.items())),
        }
        # Write via a sibling temp file so an interrupted run cannot corrupt it.
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
        temp.replace(self.path)
