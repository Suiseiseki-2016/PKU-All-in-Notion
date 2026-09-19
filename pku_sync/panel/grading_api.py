from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .exercise_grader import GRADE_ESTIMATE_LABEL, GradeBlocked, GradeSettlementError
from .exercises import EXERCISE_IDENTITY_MISSING_REASON, PAGE_IDENTITY_MISSING
from .jobs import EXERCISE_JOB_BUSY_CODE, EXERCISE_JOB_BUSY_MESSAGE, ExerciseJobGate


class GradeRequest(BaseModel):
    regrade: bool = False
    confirm: bool = False


@dataclass
class _GradeJob:
    state: str = "idle"
    job_id: str = ""
    title: str = ""
    result: dict[str, Any] | None = None


_RELAY_BLOCKED_REASONS = {
    401: "云端登录已失效，请重新激活后重试。",
    402: "AI 点不足，请兑换新额度后重试。",
    503: "AI 服务暂未配置，请联系管理员后重试。",
}
_RELAY_RETRY_REASON = "AI 服务暂时没有完成请求，请稍后重试。"


class GradeJobController:
    def __init__(self, service, gate: ExerciseJobGate | None = None):
        self.service = service
        self.gate = gate or ExerciseJobGate()
        self._lock = threading.Lock()
        self._job = _GradeJob()

    def _busy(self):
        return ({"status": "blocked", "code": EXERCISE_JOB_BUSY_CODE,
                 "message": EXERCISE_JOB_BUSY_MESSAGE, "reason": EXERCISE_JOB_BUSY_MESSAGE,
                 "retryable": True}, status.HTTP_409_CONFLICT)

    def start(self, exercise_id: str, *, regrade: bool = False, confirm: bool = False):
        if not self.gate.acquire("grade"):
            return self._busy()
        try:
            with self._lock:
                if self._job.state == "running":
                    return self._busy()
            launch = self.service.directory_service.resolve_exercise_launch(exercise_id)
            if launch.status == "failed":
                return ({"status": "failed", "reason": "打开没有成功，请稍后重试。", "fallback": None}, status.HTTP_502_BAD_GATEWAY)
            if launch.status != "opened":
                fallback = launch.fallback.model_dump() if launch.fallback else None
                return ({"status": PAGE_IDENTITY_MISSING, "reason": EXERCISE_IDENTITY_MISSING_REASON, "fallback": fallback}, status.HTTP_409_CONFLICT)
            try:
                prepared = self.service.prepare(exercise_id)
            except GradeBlocked as exc:
                if exc.reason == EXERCISE_IDENTITY_MISSING_REASON:
                    return ({"status": PAGE_IDENTITY_MISSING, "reason": exc.reason, "fallback": None}, status.HTTP_409_CONFLICT)
                return ({"status": "blocked", "reason": exc.reason, "unanswered": exc.unanswered}, status.HTTP_409_CONFLICT)
            target, grading_input = prepared
            if grading_input.marker_present and not (regrade and confirm):
                return ({"status": "confirm_required", "reason": "\u8fd9\u5957\u7ec3\u4e60\u5df2\u6709\u6279\u6539\u7ed3\u679c\uff0c\u91cd\u65b0\u6279\u6539\u524d\u8bf7\u786e\u8ba4\u3002", "estimate": GRADE_ESTIMATE_LABEL}, status.HTTP_409_CONFLICT)
            job_id = uuid.uuid4().hex
            with self._lock:
                self._job = _GradeJob(state="running", job_id=job_id, title=target.title)
            threading.Thread(target=self._execute, args=(job_id, prepared, regrade and confirm), daemon=True).start()
            return ({"status": "running", "job_id": job_id, "title": target.title, "estimate": GRADE_ESTIMATE_LABEL}, status.HTTP_202_ACCEPTED)
        finally:
            with self._lock:
                running = self._job.state == "running"
            if not running:
                self.gate.release("grade")

    def _execute(self, job_id, prepared, force=False):
        try:
            result = self.service.grade(prepared, force=force)
        except GradeBlocked as exc:
            result = {"status": "blocked", "reason": exc.reason, "unanswered": exc.unanswered}
        except GradeSettlementError as exc:
            result = {"status": "failed", "reason": exc.reason, "retryable": True,
                      "points_charged": exc.points_charged, "points_remaining": exc.points_remaining}
        except Exception as exc:
            relay_status = getattr(exc, "status_code", None)
            if relay_status in (401, 402, 503):
                result = {"status": "blocked", "reason": _RELAY_BLOCKED_REASONS[relay_status]}
            elif relay_status == 502:
                result = {"status": "failed", "reason": _RELAY_RETRY_REASON, "retryable": True}
            else:
                result = {"status": "failed", "reason": "批改没有完成，请稍后重试。"}
        with self._lock:
            if self._job.job_id == job_id:
                self._job.state = "done"
                self._job.result = result
        self.gate.release("grade")

    def snapshot(self, job_id):
        with self._lock:
            if job_id != self._job.job_id:
                return ({"status": "blocked", "reason": "找不到这次批改任务，请重试。"}, status.HTTP_404_NOT_FOUND)
            if self._job.state == "running":
                return ({"status": "running", "job_id": job_id, "title": self._job.title, "estimate": GRADE_ESTIMATE_LABEL}, status.HTTP_200_OK)
            return (dict(self._job.result or {"status": "failed", "reason": "批改状态已失效，请重试。"}), status.HTTP_200_OK)


def add_grading_routes(app, service, *, gate: ExerciseJobGate | None = None):
    controller = GradeJobController(service, gate=gate)
    app.state.grade_jobs = controller

    @app.post("/api/exercises/{exercise_id}/grade")
    def grade_exercise(exercise_id: str, request: GradeRequest | None = None, regrade: bool = False, confirm: bool = False):
        body_request = request or GradeRequest()
        body, code = controller.start(exercise_id, regrade=(regrade or body_request.regrade), confirm=(confirm or body_request.confirm))
        return JSONResponse(status_code=code, content=body)

    @app.get("/api/exercises/grade/status")
    def grade_status(job_id: str = Query(..., min_length=1)):
        body, code = controller.snapshot(job_id)
        return JSONResponse(status_code=code, content=body)


__all__ = ["GradeRequest", "GradeJobController", "add_grading_routes"]
