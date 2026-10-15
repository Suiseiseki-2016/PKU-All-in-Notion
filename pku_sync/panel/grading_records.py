from __future__ import annotations

import json

import os

import re

import tempfile

import threading

from pathlib import Path

from typing import Any

def exercise_scope(title: str) -> str:
    lecture = re.search(r"第[一二三四五六七八九十\d]+讲", title)
    if lecture:
        return lecture.group(0)
    if "考前" in title:
        return "考前练习"
    return "课程练习"

class JsonGradingRecordStore:
    """Local metadata-only grading provenance store."""
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        if self.path is not None:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    self._records = {str(k): dict(v) for k, v in payload.items() if isinstance(v, dict)}
            except (OSError, ValueError, TypeError):
                pass
    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self._records.items()}
    def put(self, exercise_id: str, record: dict[str, Any]) -> None:
        """Atomically persist the complete next snapshot before publishing it.

        A failed write leaves both the live file and in-memory view unchanged,
        which is required for pre-charge grading intent durability.
        """
        with self._lock:
            next_records = {key: dict(value) for key, value in self._records.items()}
            next_records[str(exercise_id)] = dict(record)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps(next_records, ensure_ascii=False)
                handle, temporary = tempfile.mkstemp(
                    prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
                )
                try:
                    with os.fdopen(handle, "w", encoding="utf-8") as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                except Exception:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
                    raise
            self._records = next_records

