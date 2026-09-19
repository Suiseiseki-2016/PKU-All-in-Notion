"""Metadata-only exercise directory state and local launch events."""
from __future__ import annotations
import json
import re
import threading
from pathlib import Path
from typing import Any, Iterable
EXERCISE_ORGANIZED = "organized"
EXERCISE_PENDING_ANSWER = "pending-answer"
EXERCISE_PENDING_GRADE = "pending-grade"
EXERCISE_GRADED = "graded"
EXERCISE_STATES = (EXERCISE_ORGANIZED, EXERCISE_PENDING_ANSWER, EXERCISE_PENDING_GRADE, EXERCISE_GRADED)
STATUS_LABELS = {EXERCISE_ORGANIZED: "已整理", EXERCISE_PENDING_ANSWER: "待作答", EXERCISE_PENDING_GRADE: "待批改", EXERCISE_GRADED: "已批改"}
PAGE_IDENTITY_MISSING = "page_identity_missing"
EXERCISE_IDENTITY_MISSING_TITLE = "练习页映射缺失"
EXERCISE_IDENTITY_MISSING_COPY = "该练习还没有对应的 Notion 页面，暂时无法作答或批改。可以先打开课程页查看。"
EXERCISE_IDENTITY_MISSING_REASON = "练习页映射缺失，无法确定批改目标。"

def derive_exercise_state(*, marker_present: bool, answer_present: bool, launch_started: bool) -> str:
    if marker_present:
        return EXERCISE_GRADED
    if answer_present:
        return EXERCISE_PENDING_GRADE
    if launch_started:
        return EXERCISE_PENDING_ANSWER
    return EXERCISE_ORGANIZED

def exercise_scope(title: str) -> str:
    lecture = re.search(r"第[一二三四五六七八九十\d]+讲", title)
    if lecture:
        return lecture.group(0)
    if "考前" in title:
        return "考前练习"
    return "课程练习"

_LOCAL_GRADED_STATES = {"graded", "completed", "complete", "success"}
_LOCAL_UNGRADED_STATES = {"organized", "pending", "pending-grade", "failed", "blocked"}


def _local_record_is_graded(local_record: Any) -> bool | None:
    """Read only an explicit local grading state at the panel boundary."""
    if local_record is None:
        return None
    if isinstance(local_record, bool):
        return local_record
    if isinstance(local_record, dict):
        for key in ("graded", "marker_present"):
            if isinstance(local_record.get(key), bool):
                return local_record[key]
        for key in ("status", "state"):
            value = local_record.get(key)
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in _LOCAL_GRADED_STATES:
                    return True
                if normalized in _LOCAL_UNGRADED_STATES:
                    return False
    return None


def mismatch_notice_for(entity: Any, local_record: Any = None) -> bool:
    """Return a warning only for explicit marker/record disagreement.

    The Notion marker remains authoritative for the row status.  Missing or
    malformed local state is deliberately treated as unknown, not a mismatch.
    """
    local_graded = _local_record_is_graded(local_record)
    return local_graded is not None and local_graded != bool(entity.marker_present)


def exercise_row(entity: Any, *, launch_started: bool = False, local_record: Any = None) -> dict:
    status = derive_exercise_state(marker_present=bool(entity.marker_present), answer_present=bool(entity.answer_present), launch_started=launch_started)
    row = {"id": entity.id, "url": entity.url, "title": entity.title, "course": entity.course, "scope": exercise_scope(entity.title), "status": status, "status_label": STATUS_LABELS[status], "score": None}
    if mismatch_notice_for(entity, local_record):
        row["mismatch_notice"] = True
    return row


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
        with self._lock:
            self._records[str(exercise_id)] = dict(record)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._records, ensure_ascii=False), encoding="utf-8")


class ExerciseEventStore:
    def __init__(self, path: Path | None = None, *, launched: Iterable[str] = ()):
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._launched = set(launched)
        if self.path is not None:
            self._read()
    def _read(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        values = payload.get("answer_started", []) if isinstance(payload, dict) else []
        self._launched.update(value for value in values if isinstance(value, str))
    def launched_ids(self) -> set[str]:
        with self._lock:
            return set(self._launched)
    def mark_launched(self, exercise_id: str) -> None:
        value = (exercise_id or "").strip()
        if not value:
            return
        with self._lock:
            self._launched.add(value)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"answer_started": sorted(self._launched)}, ensure_ascii=False), encoding="utf-8")

__all__ = ["EXERCISE_ORGANIZED", "EXERCISE_PENDING_ANSWER", "EXERCISE_PENDING_GRADE", "EXERCISE_GRADED", "EXERCISE_STATES", "STATUS_LABELS", "PAGE_IDENTITY_MISSING", "EXERCISE_IDENTITY_MISSING_TITLE", "EXERCISE_IDENTITY_MISSING_COPY", "EXERCISE_IDENTITY_MISSING_REASON", "derive_exercise_state", "exercise_scope", "mismatch_notice_for", "exercise_row", "ExerciseEventStore", "JsonGradingRecordStore"]
