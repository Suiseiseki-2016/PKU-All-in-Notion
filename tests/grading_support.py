from __future__ import annotations
import json
from typing import Any
from pku_sync.panel.quiz_grading_core import (
    GradeBlocked, GradeTarget, GradingInput, ANSWER_PROVENANCE_KNOWN,
    ANSWER_PROVENANCE_MARKER_ONLY, ANSWER_PROVENANCE_UNKNOWN,
)
from grading_workspace import COURSE_NET, EXERCISE_GENERATED
from grading_workspace import build_panel_workspace
from types import SimpleNamespace
from pku_sync.notion import markdown_to_blocks


class GradeRecords:
    """Only the current grader's target/record protocol; no directory UI."""
    def __init__(self):
        self.local_grading_records = {}
        workspace = build_panel_workspace()
        workspace.children[EXERCISE_GENERATED] += markdown_to_blocks(
            "## E2E_ANSWER_FIXTURE\n\n" + "\n\n".join(f"{number}. student-answer-{number}" for number in range(1, 6)))
        self.provider = SimpleNamespace(client=SimpleNamespace(_ws=workspace))

    def load(self):
        return {}

    def exercise_grade_target(self, exercise_id):
        assert exercise_id == EXERCISE_GENERATED
        return {"page_id": exercise_id, "page_url": "https://www.notion.so/" + exercise_id.replace("-", ""),
                "title": "考前练习", "course_id": COURSE_NET,
                "course_title": "计算机网络", "scope": "考前练习"}

    def record_grading(self, exercise_id, value):
        self.local_grading_records[exercise_id] = value

class FakeGradingPageAdapter:
    """In-memory grading adapter for browser verification; no Notion calls."""

    def __init__(self, *, fail_wrong_answer_preflight_once: bool = False):
        self.read_calls: list[str] = []
        self.write_calls: list[dict[str, Any]] = []
        self.marker_present = False
        self.result: dict[str, Any] | None = None
        self.wrong_answers: set[str] = set()
        self.wrong_answer_calls: list[tuple[str, str]] = []
        self._fail_wrong_answer_preflight_once = fail_wrong_answer_preflight_once

    def preflight_wrong_answer_database(self) -> None:
        if self._fail_wrong_answer_preflight_once and self.marker_present:
            self._fail_wrong_answer_preflight_once = False
            raise GradeBlocked("错题数据库暂时不可用，请稍后重试。")

    def read_for_grading(self, target: GradeTarget) -> GradingInput:
        self.read_calls.append(target.page_id)
        prompt = json.dumps({"operation": "grade", "target": {
            "page_id": target.page_id, "page_url": target.page_url,
            "course_id": target.course_id, "course_title": target.course_title,
            "scope": target.scope,
        }}, ensure_ascii=False)
        fingerprint = self.result.get("answer_fingerprint", "") if isinstance(self.result, dict) else ""
        provenance = ANSWER_PROVENANCE_KNOWN if fingerprint else (
            ANSWER_PROVENANCE_MARKER_ONLY if self.marker_present else ANSWER_PROVENANCE_UNKNOWN
        )
        return GradingInput(
            prompt=prompt, unanswered=[], marker_present=self.marker_present,
            existing_result=self.result, answer_fingerprint=fingerprint,
            answer_provenance=provenance,
        )

    def write_result(self, target: GradeTarget, *, content: str, score: float, graded_at: str) -> None:
        self.write_calls.append({"page_id": target.page_id, "content": content, "score": score, "graded_at": graded_at})
        self.marker_present = True
        self.result = {
            "status": "completed", "exercise_id": target.page_id, "title": target.title,
            "score": score, "graded_at": graded_at, "points_charged": 0,
            "points_remaining": None, "result_page_url": target.page_url,
        }

    def update_result(self, target: GradeTarget, *, content: str, score: float, graded_at: str) -> None:
        self.write_calls.append({"page_id": target.page_id, "content": content, "score": score, "graded_at": graded_at, "updated": True})
        self.marker_present = True

    def wrong_answer_exists(self, target: GradeTarget, question: dict[str, Any]) -> bool:
        key = str(question.get("wrong_answer_key") or question.get("number"))
        self.wrong_answer_calls.append(("dedup", key))
        return key in self.wrong_answers

    def create_wrong_answer(self, target: GradeTarget, question: dict[str, Any]) -> None:
        key = str(question.get("wrong_answer_key") or question.get("number"))
        self.wrong_answer_calls.append(("create", key))
        self.wrong_answers.add(key)


from pydantic import BaseModel


class PageRef(BaseModel):
    id: str
    url: str
    title: str
