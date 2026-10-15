"""Single-flight gate shared by current quiz generation and grading."""

from __future__ import annotations

import threading

EXERCISE_JOB_BUSY_CODE = "exercise_job_busy"
EXERCISE_JOB_BUSY_MESSAGE = "已有练习任务正在运行，请稍候再试。"

class ExerciseJobGate:
    """Thread-safe single-flight gate shared by organize and grading."""

    def __init__(self):
        self._lock = threading.Lock()
        self._owner = ""

    def acquire(self, kind: str) -> bool:
        with self._lock:
            if self._owner:
                return False
            self._owner = kind
            return True

    def release(self, kind: str) -> None:
        with self._lock:
            if self._owner == kind:
                self._owner = ""

    def current(self) -> str | None:
        with self._lock:
            return self._owner or None
