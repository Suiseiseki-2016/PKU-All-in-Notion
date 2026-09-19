"""One pipeline at a time, in a background thread, pollable.

The data tree and manifest are not concurrent-safe, so the panel runs a
single flight: a submit while a job is running is rejected (the caller
turns that into HTTP 409). The runner owns no pipeline logic 鈥?it calls
the injected ``run(kind) -> exit_code``, so tests drive it with a stub
and the panel wires it to the M0 service layer.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable


@dataclass
class Job:
    kind: str
    state: str = "running"  # running | done | failed
    exit_code: int | None = None
    error: str = ""
    started: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    finished: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "state": self.state,
            "exit_code": self.exit_code,
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
        }


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


class JobRunner:
    """Single-flight runner: newest request wins nothing while one runs."""

    def __init__(self, run: Callable[[str], int]) -> None:
        self._run = run
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._last: Job | None = None

    def submit(self, kind: str) -> Job | None:
        """Start ``kind`` unless one is already running; None means busy."""
        with self._lock:
            if self._job is not None and self._job.state == "running":
                return None
            job = Job(kind=kind)
            self._job = job
        threading.Thread(target=self._execute, args=(job,), daemon=True).start()
        return job

    def _execute(self, job: Job) -> None:
        try:
            code = self._run(job.kind)
        except BaseException as exc:  # the panel must survive pipeline crashes
            job.state = "failed"
            job.exit_code = 1
            job.error = f"{type(exc).__name__}: {exc}"
        else:
            job.state = "done" if code == 0 else "failed"
            job.exit_code = code
        finally:
            job.finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._lock:
                self._last = job
                self._job = None

    def current(self) -> Job | None:
        """The running job, or None when idle."""
        with self._lock:
            return self._job

    def last(self) -> Job | None:
        """The most recently finished job."""
        with self._lock:
            return self._last
