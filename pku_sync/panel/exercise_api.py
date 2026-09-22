"""Student panel organize API: static estimate, explicit confirm, blocked states."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .exercise_organizer import OrganizeBlocked
from .jobs import EXERCISE_JOB_BUSY_CODE, EXERCISE_JOB_BUSY_MESSAGE, ExerciseJobGate


class OrganizeRequest(BaseModel):
    course_id: str
    lecture_ids: list[str] = Field(default_factory=list, max_length=20)
    e2e_mode: bool = False


@dataclass
class _OrganizeJob:
    state: str = "idle"
    job_id: str = ""
    result: dict[str, Any] | None = None


class OrganizeJobController:
    """Single-flight organizer runner with a metadata-only poll result.

    ``ui_e2e_mode`` is the scratch/test-instance setting
    (EXERCISE_UI_E2E_MODE; false on every real install) that runs
    UI-driven organize requests — the student SPA's confirm sends no
    ``e2e_mode`` field — in e2e mode, so the created exercise page carries
    the ``[E2E] `` title prefix. It exists so an attended window can have
    the user drive the REAL student organize flow without mutating
    non-disposable Notion pages; the organizer's own
    ``EXERCISE_E2E_ENABLED`` gate still applies to the implied e2e call.
    """

    def __init__(self, service, gate: ExerciseJobGate | None = None,
                 ui_e2e_mode: bool = False):
        self.service = service
        self.gate = gate or ExerciseJobGate()
        self.ui_e2e_mode = bool(ui_e2e_mode)
        self._lock = threading.Lock()
        self._job = _OrganizeJob()

    def start(self, request: OrganizeRequest) -> tuple[dict, int]:
        if not self.gate.acquire("organize"):
            return ({"status": "blocked", "code": EXERCISE_JOB_BUSY_CODE,
                     "message": EXERCISE_JOB_BUSY_MESSAGE, "reason": EXERCISE_JOB_BUSY_MESSAGE,
                     "retryable": True}, status.HTTP_409_CONFLICT)
        with self._lock:
            if self._job.state == "running":
                self.gate.release("organize")
                return ({"status": "blocked", "code": EXERCISE_JOB_BUSY_CODE,
                         "message": EXERCISE_JOB_BUSY_MESSAGE, "reason": EXERCISE_JOB_BUSY_MESSAGE,
                         "retryable": True}, status.HTTP_409_CONFLICT)
            job_id = uuid.uuid4().hex
            self._job = _OrganizeJob(state="running", job_id=job_id)
        threading.Thread(target=self._execute, args=(job_id, request), daemon=True).start()
        return ({"status": "running", "job_id": job_id}, status.HTTP_202_ACCEPTED)

    def _execute(self, job_id: str, request: OrganizeRequest) -> None:
        try:
            e2e_mode = bool(request.e2e_mode) or self.ui_e2e_mode
            if e2e_mode:
                result = self.service.organize(
                    request.course_id, request.lecture_ids, e2e_mode=True
                )
            else:
                result = self.service.organize(request.course_id, request.lecture_ids)
        except OrganizeBlocked as exc:
            result = {"status": "blocked", "reason": exc.reason, "retryable": True}
            if exc.output:
                result["output"] = exc.output
            # Bounded raw relay content is attached only on an e2e-mode
            # parse failure, so normal-mode payloads stay unchanged.
            if exc.raw_content:
                result["raw_content"] = exc.raw_content
        except Exception:
            result = {"status": "blocked", "reason": "练习整理没有完成，请稍后重试。", "retryable": True}
        with self._lock:
            if self._job.job_id == job_id:
                self._job.state = "done"
                self._job.result = result
        self.gate.release("organize")

    def snapshot(self, job_id: str) -> tuple[dict, int]:
        with self._lock:
            if job_id != self._job.job_id:
                return ({"status": "blocked", "reason": "找不到这次练习整理任务，请重试。",
                         "retryable": True}, status.HTTP_404_NOT_FOUND)
            if self._job.state == "running":
                return ({"status": "running", "job_id": self._job.job_id}, status.HTTP_200_OK)
            if self._job.result is not None:
                return (dict(self._job.result), status.HTTP_200_OK)
            return ({"status": "blocked", "reason": "练习整理状态已失效，请重试。",
                     "retryable": True}, status.HTTP_409_CONFLICT)


def add_organize_routes(app, service, *, gate: ExerciseJobGate | None = None,
                        ui_e2e_mode: bool = False) -> None:
    controller = OrganizeJobController(service, gate=gate, ui_e2e_mode=ui_e2e_mode)
    app.state.organize_jobs = controller

    @app.get("/api/exercises/organize/estimate")
    def organize_estimate() -> dict:
        return service.estimate()

    @app.post("/api/exercises/organize")
    def organize_exercise(request: OrganizeRequest):
        body, code = controller.start(request)
        return JSONResponse(status_code=code, content=body)

    @app.get("/api/exercises/organize/status")
    def organize_status(job_id: str = Query(..., min_length=1)):
        body, code = controller.snapshot(job_id)
        return JSONResponse(status_code=code, content=body)


__all__ = ["OrganizeRequest", "OrganizeJobController", "add_organize_routes"]
