"""Metered exercise grading with idempotent, answer-preserving write-back."""
from __future__ import annotations

import datetime
import json
import re
import threading
from dataclasses import dataclass
from typing import Any

from ..notion import get_client, markdown_to_blocks
from .exercises import exercise_scope

GRADE_ESTIMATE_LABEL = "预计 1–5 AI 点 · 完成后按实际用量结算"
INCOMPLETE_ANSWERS_REASON = "答案尚未填写完整，请先在 Notion 完成作答。"
GRADE_MARKERS = ("批改结果", "E2E_GRADE_RESULT")


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
    marker_present: bool = False
    existing_result: dict[str, Any] | None = None


class GradeBlocked(RuntimeError):
    def __init__(self, reason: str, *, unanswered=()):
        super().__init__(reason)
        self.reason = reason
        self.unanswered = list(unanswered)


class RealGradingPageAdapter:
    """Read one selected page; write only an append/update grading section."""

    def __init__(self, settings):
        self.settings = settings

    def read_for_grading(self, target: GradeTarget) -> GradingInput:
        with get_client(self.settings) as client:
            blocks = client.list_children(target.page_id)
        prompt, unanswered = _grading_prompt(target, blocks)
        marker = _find_marker(blocks)
        return GradingInput(
            prompt=prompt,
            unanswered=unanswered,
            marker_present=marker is not None,
            existing_result=_existing_result(blocks, marker, target),
        )

    def write_result(self, target: GradeTarget, *, content: str, score: float, graded_at: str) -> None:
        self._write_result(target, content=content, score=score, graded_at=graded_at, update=False)

    def update_result(self, target: GradeTarget, *, content: str, score: float, graded_at: str) -> None:
        self._write_result(target, content=content, score=score, graded_at=graded_at, update=True)

    def _write_result(self, target: GradeTarget, *, content: str, score: float, graded_at: str, update: bool) -> None:
        rendered = markdown_to_blocks(_result_markdown(content, score=score, graded_at=graded_at))
        with get_client(self.settings) as client:
            existing = client.list_children(target.page_id)
            marker = _find_marker(existing)
            if marker is None and update:
                raise GradeBlocked("批改结果标记已失效，请刷新后重试。")
            if marker is None:
                # The first write appends the marker and its children. Existing
                # student answers are never replaced or archived.
                client.append_blocks(target.page_id, rendered)
                return
            # Keep the stable marker block and refresh only the result section.
            # Archiving stale result children is an update-in-place operation;
            # it never touches blocks before the marker (the answer area).
            marker_index = next(i for i, block in enumerate(existing) if block.get("id") == marker.get("id"))
            for block in _section_children(existing, marker_index):
                block_id = block.get("id")
                if block_id and hasattr(client, "archive_block"):
                    client.archive_block(block_id)
            client.append_blocks(target.page_id, rendered[1:])

    def wrong_answer_exists(self, target: GradeTarget, question: dict[str, Any]) -> bool:
        """Use an explicitly configured hub only; never search by title."""
        hub_id = getattr(self.settings, "notion_wrong_answer_hub_id", "")
        if not hub_id:
            return False
        with get_client(self.settings) as client:
            rows = client.list_child_pages(hub_id)
        marker = str(question.get("wrong_answer_key") or question.get("number") or "")
        return any(marker and marker in str(row.get("title") or "") for row in rows)

    def create_wrong_answer(self, target: GradeTarget, question: dict[str, Any]) -> None:
        hub_id = getattr(self.settings, "notion_wrong_answer_hub_id", "")
        if not hub_id:
            return
        title = str(question.get("wrong_answer_key") or f"{target.title} 第{question.get('number')}题")
        children = markdown_to_blocks(
            f"来源练习：{target.title}\n\n题号：{question.get('number')}\n\n题型：{question.get('type')}"
        )
        with get_client(self.settings) as client:
            client.create_page(hub_id, title, children=children)


class FakeGradingPageAdapter:
    """In-memory grading adapter for browser verification; no Notion calls."""

    def __init__(self):
        self.read_calls: list[str] = []
        self.write_calls: list[dict[str, Any]] = []
        self.marker_present = False
        self.result: dict[str, Any] | None = None
        self.wrong_answers: set[str] = set()
        self.wrong_answer_calls: list[tuple[str, str]] = []

    def read_for_grading(self, target: GradeTarget) -> GradingInput:
        self.read_calls.append(target.page_id)
        prompt = json.dumps({"operation": "grade", "target": {
            "page_id": target.page_id, "page_url": target.page_url,
            "course_id": target.course_id, "course_title": target.course_title,
            "scope": target.scope,
        }}, ensure_ascii=False)
        return GradingInput(prompt=prompt, unanswered=[], marker_present=self.marker_present, existing_result=self.result)

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


class ExerciseGrader:
    def __init__(self, *, directory_service, relay, page_adapter, clock=None):
        self.directory_service = directory_service
        self.relay = relay
        self.page_adapter = page_adapter
        self.clock = clock or _graded_now
        self._lock = threading.Lock()

    def prepare(self, exercise_id: str) -> tuple[GradeTarget, GradingInput]:
        target = self.directory_service.exercise_grade_target(exercise_id)
        if target is None:
            raise GradeBlocked("练习页映射缺失，无法确定批改目标。")
        grade_target = GradeTarget(operation="grade", page_id=target["page_id"], page_url=target["page_url"], title=target["title"], course_id=target["course_id"], course_title=target["course_title"], scope=target["scope"])
        grading_input = self.page_adapter.read_for_grading(grade_target)
        if grading_input.unanswered and not grading_input.marker_present:
            raise GradeBlocked(INCOMPLETE_ANSWERS_REASON, unanswered=grading_input.unanswered)
        return grade_target, grading_input

    def grade(self, prepared: tuple[GradeTarget, GradingInput], *, force: bool = False) -> dict[str, Any]:
        target, grading_input = prepared
        with self._lock:
            # Notion's marker is authoritative. This check occurs before quota
            # or relay access, so an unchanged rerun is free and has no LLM call.
            if grading_input.marker_present and not force:
                return _reuse_result(grading_input.existing_result, target, self.relay)
            quota = self.relay.quota()
            before = quota.get("llm_points_remaining") if quota.get("available") else None
            result = self.relay.grade(grading_input.prompt)
            charge = result.get("points_charged")
            if not isinstance(charge, (int, float)):
                raise GradeBlocked("云端没有返回本次用量，请重试。")
            score = _score(result.get("content"))
            graded_at = self.clock()
            writer = self.page_adapter.update_result if grading_input.marker_present else self.page_adapter.write_result
            writer(target, content=result["content"], score=score, graded_at=graded_at)
            for question in _parse_grade_results(result.get("content")):
                if not question.get("register_wrong"):
                    continue
                exists = getattr(self.page_adapter, "wrong_answer_exists", None)
                create = getattr(self.page_adapter, "create_wrong_answer", None)
                if exists is not None and create is not None and not exists(target, question):
                    create(target, question)
            remaining = float(before) - float(charge) if isinstance(before, (int, float)) else None
            return {"status": "completed", "exercise_id": target.page_id, "title": target.title, "score": score, "graded_at": graded_at, "points_charged": float(charge), "points_remaining": remaining, "result_page_url": target.page_url}


def _graded_now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds")


def _score(content: Any) -> float:
    try:
        value = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError) as exc:
        raise GradeBlocked("AI 返回的批改结果不完整，请重试。") from exc
    score = value.get("score") if isinstance(value, dict) else None
    if not isinstance(score, (int, float)) or not 0 <= score <= 100:
        raise GradeBlocked("AI 返回的批改结果不完整，请重试。")
    return float(score) if not float(score).is_integer() else int(score)


def _parse_grade_results(content: Any) -> list[dict[str, Any]]:
    """Normalize per-question results while keeping them out of panel payloads."""
    try:
        value = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError) as exc:
        raise GradeBlocked("AI 返回的批改结果不完整，请重试。") from exc
    rows = value.get("questions") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        return []
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            score = float(row.get("score", 0)); maximum = float(row.get("max_score", 0))
        except (TypeError, ValueError):
            continue
        if maximum <= 0:
            continue
        number = row.get("number")
        partial = 0 < score < maximum
        parsed.append({
            "number": number, "type": str(row.get("type") or ""),
            "score": score, "max_score": maximum, "partial": partial,
            # Partial responses are review material, not wrong-answer records.
            "register_wrong": score <= 0,
            "wrong_answer_key": str(row.get("wrong_answer_key") or f"{number}"),
        })
    return parsed


def _plain(block: dict) -> str:
    kind = block.get("type", ""); inner = block.get(kind) or {}
    return "".join(piece.get("plain_text") or (piece.get("text") or {}).get("content", "") for piece in inner.get("rich_text") or []).strip()


def _find_marker(blocks: list[dict]) -> dict | None:
    for block in blocks:
        if block.get("type") not in ("heading_1", "heading_2", "heading_3"):
            continue
        if _plain(block) in GRADE_MARKERS:
            return block
    return None


def _section_children(blocks: list[dict], marker_index: int) -> list[dict]:
    children: list[dict] = []
    for block in blocks[marker_index + 1:]:
        if block.get("type") in ("heading_1", "heading_2"):
            break
        children.append(block)
    return children


def _existing_result(blocks: list[dict], marker: dict | None, target: GradeTarget) -> dict[str, Any] | None:
    if marker is None:
        return None
    index = blocks.index(marker)
    section = _section_children(blocks, index)
    score = None; graded_at = ""
    for block in section:
        text = _plain(block)
        if text.startswith("总分："):
            raw = text.removeprefix("总分：").strip()
            try: score = float(raw); score = int(score) if score.is_integer() else score
            except ValueError: pass
        if text.startswith("批改时间："):
            graded_at = text.removeprefix("批改时间：").strip()
    return {"status": "completed", "exercise_id": target.page_id, "title": target.title,
            "score": score, "graded_at": graded_at, "points_charged": 0,
            "points_remaining": None, "result_page_url": target.page_url}


def _reuse_result(existing: dict[str, Any] | None, target: GradeTarget, relay=None) -> dict[str, Any]:
    """Reuse the persisted marker result without touching quota or relay."""
    if not isinstance(existing, dict):
        existing = {"status": "completed", "exercise_id": target.page_id, "title": target.title,
                    "score": None, "graded_at": "", "result_page_url": target.page_url}
    return {"status": "completed", "exercise_id": target.page_id, "title": target.title,
            "score": existing.get("score"), "graded_at": existing.get("graded_at", ""),
            "points_charged": 0.0, "points_remaining": None,
            "result_page_url": existing.get("result_page_url") or target.page_url}

def _fixture_answer(text: str) -> str:
    return re.sub(r"^\s*\d+[.、)]\s*", "", text).strip()


def _section_answers(texts: list[str]) -> list[str]:
    answers: list[str] = []
    for text in texts:
        if text.startswith("答案："):
            answers.append(text[len("答案："):].strip())
    return answers


def _grading_prompt(target: GradeTarget, blocks: list[dict]) -> tuple[str, list[str]]:
    texts = [_plain(block) for block in blocks]
    fixture_at = next((i for i, text in enumerate(texts) if text == "E2E_ANSWER_FIXTURE"), None)
    unanswered: list[str] = []
    if fixture_at is not None:
        question_count = sum(1 for block in blocks[:fixture_at] if block.get("type") == "heading_3")
        answer_blocks = blocks[fixture_at + 1:]
        answers = [_fixture_answer(text) for block, text in zip(answer_blocks, texts[fixture_at + 1:]) if block.get("type") in ("numbered_list_item", "bulleted_list_item")]
        unanswered = [f"第 {index} 题" for index, answer in enumerate(answers, 1) if not answer]
        if len(answers) < question_count:
            unanswered.extend(f"第 {number} 题" for number in range(len(answers) + 1, question_count + 1))
        unanswered = list(dict.fromkeys(unanswered))
        if not answers: unanswered = ["没有可批改的答案"]
    else:
        teacher_at = next((i for i, text in enumerate(texts) if text == "教师区（答案）"), len(texts))
        question_count = sum(1 for block in blocks[:teacher_at] if block.get("type") == "heading_3")
        answers = _section_answers(texts[:teacher_at])
        if not answers: unanswered = ["没有可批改的答案"]
        else:
            unanswered = [f"第 {index} 题" for index, answer in enumerate(answers, 1) if not answer]
            if len(answers) < question_count:
                unanswered.extend(f"第 {number} 题" for number in range(len(answers) + 1, question_count + 1))
            unanswered = list(dict.fromkeys(unanswered))
    payload = {"operation": "grade", "target": {"page_id": target.page_id, "page_url": target.page_url, "course_id": target.course_id, "course_title": target.course_title, "scope": target.scope}, "blocks": blocks}
    return json.dumps(payload, ensure_ascii=False), unanswered


def _result_markdown(content: str, *, score: float, graded_at: str) -> str:
    return f"## 批改结果\n\n批改时间：{graded_at}\n\n总分：{score}\n\n```json\n{content}\n```"


def make_grading_service(settings, directory_service, relay):
    return ExerciseGrader(directory_service=directory_service, relay=relay, page_adapter=RealGradingPageAdapter(settings))


def make_fake_grading_service(directory_service, relay):
    return ExerciseGrader(directory_service=directory_service, relay=relay, page_adapter=FakeGradingPageAdapter())


__all__ = ["GRADE_ESTIMATE_LABEL", "INCOMPLETE_ANSWERS_REASON", "GradeTarget", "GradingInput", "GradeBlocked", "RealGradingPageAdapter", "FakeGradingPageAdapter", "ExerciseGrader", "make_grading_service", "make_fake_grading_service", "_parse_grade_results"]
