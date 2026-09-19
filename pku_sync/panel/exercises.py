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

def exercise_row(entity: Any, *, launch_started: bool = False) -> dict:
    status = derive_exercise_state(marker_present=bool(entity.marker_present), answer_present=bool(entity.answer_present), launch_started=launch_started)
    row = {"id": entity.id, "url": entity.url, "title": entity.title, "course": entity.course, "scope": exercise_scope(entity.title), "status": status, "status_label": STATUS_LABELS[status], "score": None}
    if bool(getattr(entity, "mismatch_notice", False)):
        row["mismatch_notice"] = True
    return row

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

__all__ = ["EXERCISE_ORGANIZED", "EXERCISE_PENDING_ANSWER", "EXERCISE_PENDING_GRADE", "EXERCISE_GRADED", "EXERCISE_STATES", "STATUS_LABELS", "PAGE_IDENTITY_MISSING", "EXERCISE_IDENTITY_MISSING_TITLE", "EXERCISE_IDENTITY_MISSING_COPY", "EXERCISE_IDENTITY_MISSING_REASON", "derive_exercise_state", "exercise_scope", "exercise_row", "ExerciseEventStore"]
