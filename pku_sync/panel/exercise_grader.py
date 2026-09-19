"""Metered exercise grading orchestration with metadata-only panel results."""
from __future__ import annotations
import datetime
import json
import threading
from dataclasses import dataclass
from typing import Any
from ..notion import get_client, markdown_to_blocks
from .exercises import exercise_scope
GRADE_ESTIMATE_LABEL = "预计 1–5 AI 点 · 完成后按实际用量结算"
INCOMPLETE_ANSWERS_REASON = "答案尚未填写完整，请先在 Notion 完成作答。"
@dataclass(frozen=True)
class GradeTarget:
    operation: str
    page_id: str
    page_url: str
    title: str
    course_id: str
    course_title: str
    scope: str
@dataclass(frozen=True)
class GradingInput:
    prompt: str
    unanswered: list[str]
class GradeBlocked(RuntimeError):
    def __init__(self, reason: str, *, unanswered=()):
        super().__init__(reason); self.reason = reason; self.unanswered = list(unanswered)
class RealGradingPageAdapter:
    """Reads/writes only the selected exercise page; panel responses see no body data."""
    def __init__(self, settings): self.settings = settings
    def read_for_grading(self, target: GradeTarget) -> GradingInput:
        with get_client(self.settings) as client:
            blocks = client.list_children(target.page_id)
        prompt, unanswered = _grading_prompt(target, blocks)
        return GradingInput(prompt=prompt, unanswered=unanswered)
    def write_result(
        self, target: GradeTarget, *, content: str, score: float, graded_at: str
    ) -> None:
        # Core write-back is deliberately append-only. The dedicated write-back
        # feature upgrades this adapter to marker-based update-in-place.
        rendered = _result_markdown(content, score=score, graded_at=graded_at)
        with get_client(self.settings) as client:
            client.append_blocks(target.page_id, markdown_to_blocks(rendered))


class FakeGradingPageAdapter:
    """In-memory grading adapter for browser verification; no Notion calls."""
    def __init__(self):
        self.read_calls: list[str] = []
        self.write_calls: list[str] = []

    def read_for_grading(self, target: GradeTarget) -> GradingInput:
        self.read_calls.append(target.page_id)
        prompt = json.dumps({"operation": "grade", "target": {
            "page_id": target.page_id, "page_url": target.page_url,
            "course_id": target.course_id, "course_title": target.course_title,
            "scope": target.scope,
        }}, ensure_ascii=False)
        return GradingInput(prompt=prompt, unanswered=[])

    def write_result(
        self, target: GradeTarget, *, content: str, score: float, graded_at: str
    ) -> None:
        self.write_calls.append(target.page_id)


class ExerciseGrader:
    def __init__(self, *, directory_service, relay, page_adapter, clock=None):
        self.directory_service = directory_service; self.relay = relay
        self.page_adapter = page_adapter; self.clock = clock or _graded_now
        self._lock = threading.Lock()
    def prepare(self, exercise_id: str) -> tuple[GradeTarget, GradingInput]:
        target = self.directory_service.exercise_grade_target(exercise_id)
        if target is None:
            raise GradeBlocked("练习页映射缺失，无法确定批改目标。")
        grade_target = GradeTarget(operation="grade", page_id=target["page_id"], page_url=target["page_url"], title=target["title"], course_id=target["course_id"], course_title=target["course_title"], scope=target["scope"])
        grading_input = self.page_adapter.read_for_grading(grade_target)
        if grading_input.unanswered:
            raise GradeBlocked(INCOMPLETE_ANSWERS_REASON, unanswered=grading_input.unanswered)
        return grade_target, grading_input
    def grade(self, prepared: tuple[GradeTarget, GradingInput]) -> dict[str, Any]:
        target, grading_input = prepared
        with self._lock:
            quota = self.relay.quota(); before = quota.get("llm_points_remaining") if quota.get("available") else None
            result = self.relay.grade(grading_input.prompt)
            charge = result.get("points_charged")
            if not isinstance(charge, (int, float)):
                raise GradeBlocked("云端没有返回本次用量，请重试。")
            score = _score(result.get("content")); graded_at = self.clock()
            self.page_adapter.write_result(target, content=result["content"], score=score, graded_at=graded_at)
            remaining = float(before) - float(charge) if isinstance(before, (int, float)) else None
            return {"status": "completed", "exercise_id": target.page_id, "title": target.title, "score": score, "graded_at": graded_at, "points_charged": float(charge), "points_remaining": remaining, "result_page_url": target.page_url}
def _graded_now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds")
def _score(content: Any) -> float:
    try: value = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError) as exc: raise GradeBlocked("AI 返回的批改结果不完整，请重试。") from exc
    score = value.get("score") if isinstance(value, dict) else None
    if not isinstance(score, (int, float)) or not 0 <= score <= 100: raise GradeBlocked("AI 返回的批改结果不完整，请重试。")
    return float(score) if not float(score).is_integer() else int(score)
def _plain(block: dict) -> str:
    kind = block.get("type", ""); inner = block.get(kind) or {}
    return "".join(piece.get("plain_text") or (piece.get("text") or {}).get("content", "") for piece in inner.get("rich_text") or []).strip()
def _grading_prompt(target: GradeTarget, blocks: list[dict]) -> tuple[str, list[str]]:
    texts = [_plain(block) for block in blocks]
    fixture_at = next((i for i, text in enumerate(texts) if text == "E2E_ANSWER_FIXTURE"), None)
    unanswered: list[str] = []
    if fixture_at is not None:
        question_count = sum(1 for block in blocks[:fixture_at] if block.get("type") == "heading_3")
        answers = [text for block, text in zip(blocks[fixture_at + 1:], texts[fixture_at + 1:]) if block.get("type") in ("numbered_list_item", "bulleted_list_item") and text]
        if not answers: unanswered = ["没有可批改的答案"]
        elif question_count > len(answers): unanswered = [f"第 {number} 题" for number in range(len(answers) + 1, question_count + 1)]
    else:
        teacher_at = next((i for i, text in enumerate(texts) if text == "教师区（答案）"), len(texts))
        question_count = sum(1 for block in blocks[:teacher_at] if block.get("type") == "heading_3")
        answers = [text for text in texts[:teacher_at] if text.startswith("答案：") and text[3:].strip()]
        if not answers: unanswered = ["没有可批改的答案"]
        elif question_count > len(answers): unanswered = [f"第 {number} 题" for number in range(len(answers) + 1, question_count + 1)]
    payload = {"operation": "grade", "target": {"page_id": target.page_id, "page_url": target.page_url, "course_id": target.course_id, "course_title": target.course_title, "scope": target.scope}, "blocks": blocks}
    return json.dumps(payload, ensure_ascii=False), unanswered
def _result_markdown(content: str, *, score: float, graded_at: str) -> str:
    return f"## 批改结果\n\n批改时间：{graded_at}\n\n总分：{score}\n\n```json\n{content}\n```"
def make_grading_service(settings, directory_service, relay):
    return ExerciseGrader(directory_service=directory_service, relay=relay, page_adapter=RealGradingPageAdapter(settings))

def make_fake_grading_service(directory_service, relay):
    return ExerciseGrader(
        directory_service=directory_service,
        relay=relay,
        page_adapter=FakeGradingPageAdapter(),
    )


__all__ = ["GRADE_ESTIMATE_LABEL", "INCOMPLETE_ANSWERS_REASON", "GradeTarget", "GradingInput", "GradeBlocked", "RealGradingPageAdapter", "FakeGradingPageAdapter", "ExerciseGrader", "make_grading_service", "make_fake_grading_service"]
