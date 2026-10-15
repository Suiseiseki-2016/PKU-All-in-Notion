"""Native teaching-portal course quiz routes, separate from legacy title mapping."""
import logging

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .quiz_contract import OrganizeBlocked
from .jobs import EXERCISE_JOB_BUSY_MESSAGE
from .quiz_grading_core import GradeBlocked, GradeSettlementError

logger = logging.getLogger(__name__)

class GenerateQuizRequest(BaseModel):
    lecture_ids: list[str] = Field(default_factory=list, max_length=50)
    confirmed: bool = False
    prior_paper: str = Field(default="", max_length=20000)

class GradeQuizRequest(BaseModel):
    confirmed: bool = False
    force: bool = False

class ReviewRequest(BaseModel):
    confirmed: bool = False
    note: str = Field(default="", max_length=4000)

def add_campus_quiz_routes(app, service, gate, grading_service=None):
    @app.get("/api/campus/courses/{course_id}/quizzes")
    def quizzes(course_id: str):
        try:
            return service.list(course_id)
        except OrganizeBlocked as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Native course quizzes could not be read")
            raise HTTPException(status_code=502, detail="小测中心暂时无法读取，请检查新版 Notion 连接。") from exc

    @app.post("/api/campus/courses/{course_id}/quizzes")
    def generate(course_id: str, body: GenerateQuizRequest):
        if not gate.acquire("campus-quiz"):
            raise HTTPException(status_code=409, detail=EXERCISE_JOB_BUSY_MESSAGE)
        try:
            return service.generate(course_id, body.lecture_ids, confirmed=body.confirmed, prior_paper=body.prior_paper)
        except OrganizeBlocked as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Native course quiz generation did not complete")
            raise HTTPException(status_code=502, detail="小测尚未完成，恢复记录已保留；请刷新后核对，避免重复生成。") from exc
        finally:
            gate.release("campus-quiz")

    if grading_service is None:
        return

    @app.post("/api/campus/courses/{course_id}/quizzes/{quiz_id}/grade")
    def grade(course_id: str, quiz_id: str, body: GradeQuizRequest):
        if not gate.acquire("campus-grade"):
            raise HTTPException(status_code=409, detail=EXERCISE_JOB_BUSY_MESSAGE)
        try:
            return grading_service.grade(course_id, quiz_id, confirmed=body.confirmed, force=body.force)
        except GradeSettlementError as exc:
            return JSONResponse(status_code=502, content={"detail": exc.reason,
                "points_charged": exc.points_charged, "points_remaining": exc.points_remaining})
        except GradeBlocked as exc:
            return JSONResponse(status_code=409, content={"detail": exc.reason, "unanswered": exc.unanswered})
        except OrganizeBlocked as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc
        except Exception as exc:
            logger.exception("Native quiz grade did not complete")
            raise HTTPException(status_code=502, detail="批改未完成，恢复记录已保留；请核对后恢复。") from exc
        finally:
            gate.release("campus-grade")

    @app.get("/api/campus/courses/{course_id}/quiz-reviews")
    def reviews(course_id: str):
        try:
            return grading_service.reviews(course_id)
        except (GradeBlocked, OrganizeBlocked) as exc:
            raise HTTPException(status_code=409, detail=exc.reason) from exc
        except Exception as exc:
            logger.exception("Native quiz reviews could not be read")
            raise HTTPException(status_code=502, detail="复习记录暂时无法读取。") from exc

    @app.post("/api/campus/courses/{course_id}/quiz-reviews/{review_id}")
    def record_review(course_id: str, review_id: str, body: ReviewRequest):
        try:
            return grading_service.record_review(course_id, review_id, note=body.note, confirmed=body.confirmed)
        except (GradeBlocked, OrganizeBlocked) as exc:
            raise HTTPException(status_code=409, detail=exc.reason) from exc
        except Exception as exc:
            logger.exception("Native quiz review history write did not complete")
            raise HTTPException(status_code=502, detail="复习记录未完成，请刷新后核对。") from exc
