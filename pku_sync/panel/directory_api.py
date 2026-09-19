"""Student panel directory API routes (VAL-META-015..018).

Identity-only launch, honest sync states, and the three material-view
groupings — added to the loopback panel by ``webapi.create_app``. Every
user-facing message is generic (no token, path, or content fragment), all
launch resolution happens against the last loaded directory (zero client
calls, zero search), and failed syncs never report ``done``.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..notion_meta import LAUNCH_MISSING_MAPPING, LAUNCH_OPENED
from .exercises import (
    EXERCISE_IDENTITY_MISSING_COPY,
    EXERCISE_IDENTITY_MISSING_REASON,
    PAGE_IDENTITY_MISSING,
)
from .directory import (
    LAUNCH_FAILED,
    LAUNCH_FAILED_COPY,
    LAUNCH_MISSING_MAPPING_COPY,
    MATERIAL_VIEWS,
    DirectoryApiError,
    DirectoryService,
)

logger = logging.getLogger(__name__)


class ExerciseLaunchRequest(BaseModel):
    """Exercise launch intent; the URL is always resolved server-side."""

    purpose: str = Field(pattern="^(answer|result)$")

class LaunchRequest(BaseModel):
    """Identity-only launch payload: the stored target id and optional
    course context for the fallback. No token, no URL, no search query."""

    target_id: str
    target_type: str = "page"
    course_id: str | None = None


def add_directory_routes(app, service: DirectoryService) -> None:
    """Mount the directory/sync/materials/launch endpoints on the panel app."""

    @app.get("/api/directory")
    def directory_endpoint() -> dict:
        try:
            return service.load()
        except DirectoryApiError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    @app.get("/api/exercises")
    def exercises_endpoint() -> dict:
        try:
            payload = service.load()
        except DirectoryApiError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
        return {"exercises": payload["exercises"]}

    @app.post("/api/exercises/{exercise_id}/answer-started")
    def exercise_answer_started_endpoint(exercise_id: str) -> dict:
        service.mark_exercise_launched(exercise_id)
        return {"status": "recorded", "exercise_id": exercise_id}

    @app.post("/api/exercises/{exercise_id}/launch")
    def exercise_launch_endpoint(exercise_id: str, request: ExerciseLaunchRequest):
        result = service.resolve_exercise_launch(exercise_id)
        if result.status == LAUNCH_OPENED:
            if request.purpose == "answer":
                service.mark_exercise_launched(exercise_id)
            logger.info("panel exercise launch resolved url=%s", result.url)
            return {
                "status": LAUNCH_OPENED,
                "target_id": result.target_id,
                "url": result.url,
                "fallback": None,
            }
        if result.status == LAUNCH_FAILED:
            logger.info("panel exercise launch failed target_id=%s", result.target_id)
            return JSONResponse(status_code=502, content={"detail": LAUNCH_FAILED_COPY})
        fallback = result.fallback.model_dump() if result.fallback else None
        logger.info("panel exercise launch missing-mapping target_id=%s", result.target_id)
        return JSONResponse(
            status_code=404,
            content={
                "detail": EXERCISE_IDENTITY_MISSING_COPY,
                "status": PAGE_IDENTITY_MISSING,
                "target_id": result.target_id,
                "url": None,
                "fallback": fallback,
            },
        )

    @app.post("/api/exercises/{exercise_id}/grade")
    def exercise_grade_identity_guard(exercise_id: str):
        result = service.resolve_exercise_launch(exercise_id)
        if result.status == LAUNCH_OPENED:
            return JSONResponse(
                status_code=501,
                content={
                    "status": "grading_not_available",
                    "reason": "批改服务尚未就绪。",
                },
            )
        if result.status == LAUNCH_FAILED:
            return JSONResponse(
                status_code=502,
                content={
                    "status": LAUNCH_FAILED,
                    "reason": LAUNCH_FAILED_COPY,
                    "fallback": None,
                },
            )
        fallback = result.fallback.model_dump() if result.fallback else None
        return JSONResponse(
            status_code=409,
            content={
                "status": PAGE_IDENTITY_MISSING,
                "reason": EXERCISE_IDENTITY_MISSING_REASON,
                "fallback": fallback,
            },
        )
    @app.get("/api/sync/state")
    def sync_state_endpoint() -> dict:
        return service.sync_snapshot()

    @app.get("/api/courses/{course_id}/materials")
    def materials_endpoint(course_id: str, view: str, lecture_id: str | None = None) -> dict:
        if view not in MATERIAL_VIEWS:
            raise HTTPException(status_code=400, detail=f"未知的资料视图：{view}。")
        try:
            return service.material_view(course_id, view, lecture_id=lecture_id)
        except DirectoryApiError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    @app.post("/api/launch")
    def launch_endpoint(request: LaunchRequest) -> dict:
        result = service.resolve_launch(request.target_id, course_id=request.course_id)
        if result.status == LAUNCH_OPENED:
            # resolved-URL evidence for launch verification: the stored
            # canonical identity only — never a token or local path
            logger.info("panel launch resolved url=%s", result.url)
            return {
                "status": LAUNCH_OPENED,
                "target_id": result.target_id,
                "url": result.url,
                "fallback": None,
            }
        if result.status == LAUNCH_FAILED:
            logger.info("panel launch failed target_id=%s", result.target_id)
            return JSONResponse(
                status_code=502,
                content={"detail": LAUNCH_FAILED_COPY},
            )
        # explicit missing-mapping state with the course-level fallback; 4xx
        # carries only generic copy plus stored identity (no fabricated URL)
        fallback = result.fallback.model_dump() if result.fallback else None
        logger.info("panel launch missing-mapping target_id=%s", result.target_id)
        return JSONResponse(
            status_code=404,
            content={
                "detail": LAUNCH_MISSING_MAPPING_COPY,
                "status": LAUNCH_MISSING_MAPPING,
                "target_id": result.target_id,
                "url": None,
                "fallback": fallback,
            },
        )
