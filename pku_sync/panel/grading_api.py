"""Asynchronous grading API with preflight validation and metadata-only results."""
from __future__ import annotations
import threading
import uuid
from dataclasses import dataclass
from typing import Any
from fastapi import Query, status
from fastapi.responses import JSONResponse
from .exercise_grader import GRADE_ESTIMATE_LABEL, GradeBlocked
from .exercises import EXERCISE_IDENTITY_MISSING_REASON, PAGE_IDENTITY_MISSING
@dataclass
class _GradeJob:
    state: str = "idle"
    job_id: str = ""
    title: str = ""
    result: dict[str, Any] | None = None
class GradeJobController:
    def __init__(self, service): self.service = service; self._lock = threading.Lock(); self._job = _GradeJob()
    def start(self, exercise_id: str):
        with self._lock:
            if self._job.state == "running": return ({"status": "blocked", "reason": "上一次批改还在进行，请稍候再试。"}, status.HTTP_409_CONFLICT)
        # Preserve the identity-only launch contract before any page read.
        launch = self.service.directory_service.resolve_exercise_launch(exercise_id)
        if launch.status == "failed":
            return ({"status": "failed", "reason": "打开没有成功，请稍后重试。", "fallback": None}, status.HTTP_502_BAD_GATEWAY)
        if launch.status != "opened":
            fallback = launch.fallback.model_dump() if launch.fallback else None
            return ({"status": PAGE_IDENTITY_MISSING, "reason": EXERCISE_IDENTITY_MISSING_REASON, "fallback": fallback}, status.HTTP_409_CONFLICT)
        try: prepared = self.service.prepare(exercise_id)
        except GradeBlocked as exc:
            if exc.reason == EXERCISE_IDENTITY_MISSING_REASON:
                return ({"status": PAGE_IDENTITY_MISSING, "reason": exc.reason, "fallback": None}, status.HTTP_409_CONFLICT)
            return ({"status": "blocked", "reason": exc.reason, "unanswered": exc.unanswered}, status.HTTP_409_CONFLICT)
        target, _ = prepared; job_id = uuid.uuid4().hex
        with self._lock: self._job = _GradeJob(state="running", job_id=job_id, title=target.title)
        threading.Thread(target=self._execute, args=(job_id, prepared), daemon=True).start()
        return ({"status": "running", "job_id": job_id, "title": target.title, "estimate": GRADE_ESTIMATE_LABEL}, status.HTTP_202_ACCEPTED)
    def _execute(self, job_id, prepared):
        try: result = self.service.grade(prepared)
        except GradeBlocked as exc: result = {"status": "blocked", "reason": exc.reason, "unanswered": exc.unanswered}
        except Exception: result = {"status": "failed", "reason": "批改没有完成，请稍后重试。"}
        with self._lock:
            if self._job.job_id == job_id: self._job.state = "done"; self._job.result = result
    def snapshot(self, job_id):
        with self._lock:
            if job_id != self._job.job_id: return ({"status": "blocked", "reason": "找不到这次批改任务，请重试。"}, status.HTTP_404_NOT_FOUND)
            if self._job.state == "running": return ({"status": "running", "job_id": job_id, "title": self._job.title, "estimate": GRADE_ESTIMATE_LABEL}, status.HTTP_200_OK)
            return (dict(self._job.result or {"status": "failed", "reason": "批改状态已失效，请重试。"}), status.HTTP_200_OK)
def add_grading_routes(app, service):
    controller = GradeJobController(service); app.state.grade_jobs = controller
    @app.post("/api/exercises/{exercise_id}/grade")
    def grade_exercise(exercise_id: str):
        body, code = controller.start(exercise_id); return JSONResponse(status_code=code, content=body)
    @app.get("/api/exercises/grade/status")
    def grade_status(job_id: str = Query(..., min_length=1)):
        body, code = controller.snapshot(job_id); return JSONResponse(status_code=code, content=body)
__all__ = ["GradeJobController", "add_grading_routes"]
