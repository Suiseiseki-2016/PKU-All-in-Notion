"""Metered exercise grading with idempotent, answer-preserving write-back."""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from typing import Any

from ..notion import get_client, markdown_to_blocks
from ..notion_meta import ANSWER_AREA_MARKERS, GRADING_MARKERS
from .exercises import exercise_scope

GRADE_ESTIMATE_LABEL = "预计 1–5 AI 点 · 完成后按实际用量结算"
INCOMPLETE_ANSWERS_REASON = "答案尚未填写完整，请先在 Notion 完成作答。"
GRADE_MARKERS = ("批改结果", "E2E_GRADE_RESULT")
ANSWER_PROVENANCE_KNOWN = "known"
ANSWER_PROVENANCE_MARKER_ONLY = "marker-only"
ANSWER_PROVENANCE_UNKNOWN = "unknown"


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
    answer_fingerprint: str = ""
    answer_provenance: str | None = None


class GradeBlocked(RuntimeError):
    def __init__(self, reason: str, *, unanswered=()):
        super().__init__(reason)
        self.reason = reason
        self.unanswered = list(unanswered)


class GradeSettlementError(RuntimeError):
    """A failure after relay metering, with authoritative settlement."""
    def __init__(self, reason: str, *, points_charged: float, points_remaining: float | None):
        super().__init__(reason)
        self.reason = reason
        self.points_charged = float(points_charged)
        self.points_remaining = points_remaining


class RealGradingPageAdapter:
    """Read one selected page; write only an append/update grading section."""

    def __init__(self, settings):
        self.settings = settings

    def read_for_grading(self, target: GradeTarget) -> GradingInput:
        with get_client(self.settings) as client:
            blocks = client.list_children(target.page_id)
        prompt, unanswered = _grading_prompt(target, blocks)
        marker = _find_marker(blocks)
        provenance, fingerprint = _answer_provenance(blocks)
        return GradingInput(
            prompt=prompt,
            unanswered=unanswered,
            marker_present=marker is not None,
            existing_result=_existing_result(blocks, marker, target),
            answer_fingerprint=fingerprint,
            answer_provenance=provenance,
        )

    def verify_parent(self, target: GradeTarget) -> None:
        with get_client(self.settings) as client:
            page = client.get_page(target.page_id)
        parent = page.get("parent") if isinstance(page, dict) else None
        parent_id = (parent or {}).get("page_id") if isinstance(parent, dict) else None
        if not parent_id or not _same_page_id(parent_id, target.course_id):
            raise GradeBlocked("\u7ec3\u4e60\u9875\u5df2\u79fb\u51fa\u5f53\u524d\u8bfe\u7a0b\uff0c\u65e0\u6cd5\u6279\u6539\uff0c\u8bf7\u5237\u65b0\u76ee\u5f55\u540e\u91cd\u8bd5\u3002")

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


class ExerciseGrader:
    def __init__(self, *, directory_service, relay, page_adapter, clock=None):
        self.directory_service = directory_service
        self.relay = relay
        self.page_adapter = page_adapter
        self.clock = clock or _graded_now
        self._lock = threading.Lock()
        # Keep charged operations until Notion and local provenance both settle.
        self._pending: dict[str, dict[str, Any]] = {}

    def prepare(self, exercise_id: str) -> tuple[GradeTarget, GradingInput]:
        target = self.directory_service.exercise_grade_target(exercise_id)
        if target is None:
            raise GradeBlocked("练习页映射缺失，无法确定批改目标。")
        grade_target = GradeTarget(operation="grade", page_id=target["page_id"], page_url=target["page_url"], title=target["title"], course_id=target["course_id"], course_title=target["course_title"], scope=target["scope"])
        verify_parent = getattr(self.page_adapter, "verify_parent", None)
        if verify_parent is not None:
            verify_parent(grade_target)
        grading_input = self.page_adapter.read_for_grading(grade_target)
        if grading_input.unanswered and not grading_input.marker_present:
            raise GradeBlocked(INCOMPLETE_ANSWERS_REASON, unanswered=grading_input.unanswered)
        return grade_target, grading_input

    def grade(self, prepared: tuple[GradeTarget, GradingInput], *, force: bool = False) -> dict[str, Any]:
        target, grading_input = prepared
        with self._lock:
            pending = self._pending.get(target.page_id)
            if pending is None:
                records = getattr(self.directory_service, "local_grading_records", {})
                candidate = records.get(target.page_id) if isinstance(records, dict) else None
                if isinstance(candidate, dict) and candidate.get("status") == "pending":
                    pending = dict(candidate)
            local_record = getattr(self.directory_service, "local_grading_records", {}).get(target.page_id)
            previous_fingerprint = local_record.get("answer_fingerprint") if isinstance(local_record, dict) else ""
            provenance = _resolved_provenance(grading_input)
            fingerprints_equal = bool(
                provenance == ANSWER_PROVENANCE_KNOWN
                and grading_input.answer_fingerprint
                and previous_fingerprint
                and grading_input.answer_fingerprint == previous_fingerprint
            )
            if pending is not None:
                return self._retry_pending(target, grading_input, pending)
            if grading_input.marker_present:
                if provenance == ANSWER_PROVENANCE_MARKER_ONLY:
                    return _reuse_result(
                        grading_input.existing_result, target, settlement_known=_has_known_settlement(local_record)
                    )
                if fingerprints_equal:
                    return _reuse_result(
                        grading_input.existing_result, target, settlement_known=True
                    )
                if not force:
                    raise GradeBlocked("\u65e0\u6cd5\u786e\u8ba4\u7b54\u6848\u662f\u5426\u672a\u53d8\uff0c\u8bf7\u786e\u8ba4\u91cd\u65b0\u6279\u6539\u3002")
            quota = self.relay.quota()
            before = quota.get("llm_points_remaining") if quota.get("available") else None
            result = self.relay.grade(grading_input.prompt)
            charge = result.get("points_charged")
            if not isinstance(charge, (int, float)):
                raise GradeBlocked("\u4e91\u7aef\u6ca1\u6709\u8fd4\u56de\u6709\u6548\u7684\u7528\u91cf\u7ed3\u7b97\uff0c\u8bf7\u91cd\u8bd5\u3002")
            remaining = float(before) - float(charge) if isinstance(before, (int, float)) else None
            try:
                score = _score(result.get("content"))
                graded_at = self.clock()
                pending = {"status": "pending", "marker_present": False,
                           "answer_fingerprint": grading_input.answer_fingerprint,
                           "score": score, "graded_at": graded_at,
                           "answer_provenance": ANSWER_PROVENANCE_KNOWN,
                           "content": result["content"], "points_charged": float(charge),
                           "points_remaining": remaining, "result_page_url": target.page_url}
                self._pending[target.page_id] = pending
                self._persist_record(target.page_id, pending)
                self._write_and_register(target, grading_input, pending)
                completed = dict(pending)
                completed["status"] = "graded"
                completed["marker_present"] = True
                self._persist_record(target.page_id, completed)
                self._pending.pop(target.page_id, None)
                return _summary(target, completed)
            except GradeBlocked as exc:
                raise GradeSettlementError(exc.reason, points_charged=float(charge), points_remaining=_authoritative_balance(self.relay, remaining, 0)) from exc
            except Exception as exc:
                raise GradeSettlementError("\u6279\u6539\u7ed3\u679c\u5199\u56de Notion \u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002", points_charged=float(charge), points_remaining=_authoritative_balance(self.relay, remaining, 0)) from exc

    def _persist_record(self, exercise_id: str, record: dict[str, Any]) -> None:
        persist = getattr(self.directory_service, "record_grading", None)
        if persist is not None:
            persist(exercise_id, record)

    def _write_and_register(self, target: GradeTarget, grading_input: GradingInput, pending: dict[str, Any]) -> None:
        writer = self.page_adapter.update_result if grading_input.marker_present else self.page_adapter.write_result
        writer(target, content=pending["content"], score=pending["score"], graded_at=pending["graded_at"])
        for question in _parse_grade_results(pending["content"]):
            if not question.get("register_wrong"):
                continue
            exists = getattr(self.page_adapter, "wrong_answer_exists", None)
            create = getattr(self.page_adapter, "create_wrong_answer", None)
            if exists is not None and create is not None and not exists(target, question):
                create(target, question)

    def _retry_pending(self, target: GradeTarget, grading_input: GradingInput, pending: dict[str, Any]) -> dict[str, Any]:
        """Finish a previously charged operation without another relay call."""
        try:
            self._pending[target.page_id] = dict(pending)
            self._write_and_register(target, grading_input, pending)
            completed = dict(pending)
            completed["status"] = "graded"
            completed["marker_present"] = True
            self._persist_record(target.page_id, completed)
            self._pending.pop(target.page_id, None)
            return _summary(target, completed)
        except GradeBlocked as exc:
            raise GradeSettlementError(exc.reason, points_charged=float(pending.get("points_charged", 0)), points_remaining=pending.get("points_remaining")) from exc
        except Exception as exc:
            raise GradeSettlementError("\u6279\u6539\u7ed3\u679c\u5199\u56de Notion \u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002", points_charged=float(pending.get("points_charged", 0)), points_remaining=pending.get("points_remaining")) from exc


def _summary(target: GradeTarget, record: dict[str, Any]) -> dict[str, Any]:
    return {"status": "completed", "exercise_id": target.page_id, "title": target.title,
            "score": record.get("score"), "graded_at": record.get("graded_at", ""),
            "points_charged": float(record.get("points_charged", 0)),
            "points_remaining": record.get("points_remaining"),
            "result_page_url": record.get("result_page_url") or target.page_url}


def _graded_now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds")


def _authoritative_balance(relay, before, charge: float) -> float | None:
    try:
        value = relay.quota().get("llm_points_remaining")
        if isinstance(value, (int, float)):
            return float(value)
    except Exception:
        pass
    return float(before) - charge if isinstance(before, (int, float)) else None


def _same_page_id(left: str, right: str) -> bool:
    normalize = lambda value: "".join(ch for ch in str(value).lower() if ch.isalnum())
    return normalize(left) == normalize(right)


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


def _has_known_settlement(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and record.get("status") == "graded"
        and isinstance(record.get("points_charged"), (int, float))
    )


def _reuse_result(
    existing: dict[str, Any] | None,
    target: GradeTarget,
    *,
    settlement_known: bool,
) -> dict[str, Any]:
    """Reuse the marker result without quota access or fabricated settlement."""
    if not isinstance(existing, dict):
        existing = {"status": "completed", "exercise_id": target.page_id, "title": target.title,
                    "score": None, "graded_at": "", "result_page_url": target.page_url}
    return {"status": "completed", "exercise_id": target.page_id, "title": target.title,
            "score": existing.get("score"), "graded_at": existing.get("graded_at", ""),
            "points_charged": 0.0 if settlement_known else None,
            "points_remaining": None,
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
    fixture_at = next((i for i, text in enumerate(texts) if text in ANSWER_AREA_MARKERS), None)
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


def _answer_provenance(blocks: list[dict]) -> tuple[str, str]:
    """Return answer provenance and a canonical answer-only fingerprint.

    The grading section is excluded. A marker with no remaining question or
    answer structure is a legitimate marker-only reconstruction. If question
    structure remains but answers were deleted or use an unsupported shape,
    provenance is unknown rather than silently equal to an earlier grade.
    """
    marker = _find_marker(blocks)
    marker_index = blocks.index(marker) if marker is not None else len(blocks)
    source = blocks[:marker_index]
    texts = [_plain(block) for block in source]
    question_count = sum(1 for block in source if block.get("type") == "heading_3")
    fixture_at = next((i for i, value in enumerate(texts) if value in ANSWER_AREA_MARKERS), None)

    if fixture_at is not None:
        rows = [
            _fixture_answer(text)
            for block, text in zip(source[fixture_at + 1:], texts[fixture_at + 1:])
            if block.get("type") in ("numbered_list_item", "bulleted_list_item")
        ]
        if rows:
            canonical = _canonical_answers(rows, question_count)
            return ANSWER_PROVENANCE_KNOWN, _fingerprint(canonical)
        return ANSWER_PROVENANCE_UNKNOWN, ""

    teacher_at = next((i for i, value in enumerate(texts) if value == "\u6559\u5e08\u533a\uff08\u7b54\u6848\uff09"), len(source))
    answer_rows = _section_answers(texts[:teacher_at])
    if answer_rows:
        canonical = _canonical_answers(answer_rows, question_count)
        return ANSWER_PROVENANCE_KNOWN, _fingerprint(canonical)
    if marker is not None and question_count == 0 and not any(texts):
        return ANSWER_PROVENANCE_MARKER_ONLY, ""
    return ANSWER_PROVENANCE_UNKNOWN, ""


def _canonical_answers(answers: list[str], question_count: int) -> list[str]:
    count = max(question_count, len(answers))
    return [answers[index] if index < len(answers) else "<missing>" for index in range(count)]


def _fingerprint(answers: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(answers, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _resolved_provenance(grading_input: GradingInput) -> str:
    if grading_input.answer_provenance in {
        ANSWER_PROVENANCE_KNOWN,
        ANSWER_PROVENANCE_MARKER_ONLY,
        ANSWER_PROVENANCE_UNKNOWN,
    }:
        return grading_input.answer_provenance
    if grading_input.answer_fingerprint:
        return ANSWER_PROVENANCE_KNOWN
    return ANSWER_PROVENANCE_UNKNOWN


def _answer_fingerprint(blocks: list[dict]) -> str:
    """Compatibility wrapper for callers that only need the digest."""
    return _answer_provenance(blocks)[1]


def _result_markdown(content: str, *, score: float, graded_at: str) -> str:
    return f"## 批改结果\n\n批改时间：{graded_at}\n\n总分：{score}\n\n```json\n{content}\n```"


def make_grading_service(settings, directory_service, relay):
    return ExerciseGrader(directory_service=directory_service, relay=relay, page_adapter=RealGradingPageAdapter(settings))


def make_fake_grading_service(directory_service, relay):
    return ExerciseGrader(directory_service=directory_service, relay=relay, page_adapter=FakeGradingPageAdapter())


__all__ = ["ANSWER_PROVENANCE_KNOWN", "ANSWER_PROVENANCE_MARKER_ONLY", "ANSWER_PROVENANCE_UNKNOWN", "GRADE_ESTIMATE_LABEL", "INCOMPLETE_ANSWERS_REASON", "GradeTarget", "GradingInput", "GradeBlocked", "RealGradingPageAdapter", "FakeGradingPageAdapter", "ExerciseGrader", "make_grading_service", "make_fake_grading_service", "_parse_grade_results"]
