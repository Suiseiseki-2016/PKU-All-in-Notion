"""Metered exercise grading with idempotent, answer-preserving write-back."""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import math
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from ..notion import get_client, markdown_to_blocks, prop_title
from ..notion_meta import ANSWER_AREA_MARKERS, GRADING_MARKERS
from .exercises import exercise_scope
from .llm_json import unwrap_single_json_fence

GRADE_ESTIMATE_LABEL = "预计 1–5 AI 点 · 完成后按实际用量结算"
INCOMPLETE_ANSWERS_REASON = "答案尚未填写完整，请先在 Notion 完成作答。"
GRADE_MARKERS = ("批改结果", "E2E_GRADE_RESULT")
GRADE_RESULT_CONTRACT_VERSION = "pku-e2e-grade-v1"
GRADE_RESULT_INVALID_REASON = "AI 返回的批改结果不完整，请重试。"


@dataclass(frozen=True)
class GradeQuestionRubric:
    question_id: str
    number: int
    type: str
    max_score: int
    expected_score: int
    expected_outcome: str
    register_wrong: bool

    def provider_schema(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id, "number": self.number,
            "type": self.type, "max_score": self.max_score,
            "expected_score": self.expected_score,
            "expected_outcome": self.expected_outcome,
            "register_wrong": self.register_wrong,
        }


E2E_GRADE_QUESTION_RUBRIC = (
    GradeQuestionRubric("Q1", 1, "选择", 2, 2, "correct", False),
    GradeQuestionRubric("Q2", 2, "判断", 2, 0, "wrong", True),
    GradeQuestionRubric("Q3", 3, "填空", 2, 1, "partial", False),
    GradeQuestionRubric("Q4", 4, "简答", 2, 2, "correct", False),
    GradeQuestionRubric("Q5", 5, "论述", 2, 0, "wrong", True),
)
ANSWER_PROVENANCE_KNOWN = "known"
ANSWER_PROVENANCE_MARKER_ONLY = "marker-only"
ANSWER_PROVENANCE_UNKNOWN = "unknown"
PHASE_INTENT = "intent"
PHASE_RELAY_STARTED = "relay_started"
PHASE_RELAY_SUCCEEDED = "relay_succeeded"
PHASE_RESULT_VERIFIED = "result_verified"
PHASE_RESULT_RECONCILED = "result_reconciled"
PHASE_WRONG_ANSWERS_VERIFIED = "wrong_answers_verified"
PHASE_COMPLETED = "completed"
_NONTERMINAL_PHASES = {
    PHASE_INTENT, PHASE_RELAY_STARTED, PHASE_RELAY_SUCCEEDED,
    PHASE_RESULT_VERIFIED, PHASE_RESULT_RECONCILED,
    PHASE_WRONG_ANSWERS_VERIFIED,
}


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
    result_marker: str = GRADE_MARKERS[0]
    result_contract: str | None = None


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

    def __init__(self, settings, directory_service=None):
        self.settings = settings
        self.directory_service = directory_service
        self._wrong_answer_database = None
        self._wrong_answer_title_property = ""

    def preflight_wrong_answer_database(self) -> None:
        """Resolve identity and real title property before any relay charge."""
        # Resolve against the current directory generation for every grading
        # prepare. A refreshed workspace must never inherit a stale target.
        if self.directory_service is None:
            raise GradeBlocked("\u9519\u9898\u6570\u636e\u5e93\u4e0d\u53ef\u7528\uff0c\u8bf7\u5237\u65b0\u76ee\u5f55\u5e76\u68c0\u67e5 Notion \u8bbe\u7f6e\u3002")
        try:
            identity = self.directory_service.wrong_answer_database_identity()
        except Exception as exc:
            reason = getattr(exc, "message", str(exc))
            raise GradeBlocked(reason or "\u9519\u9898\u6570\u636e\u5e93\u4e0d\u53ef\u7528\uff0c\u8bf7\u5237\u65b0\u76ee\u5f55\u5e76\u68c0\u67e5 Notion \u8bbe\u7f6e\u3002") from exc
        with get_client(self.settings) as client:
            schema = client.get_database(identity.id)
        properties = schema.get("properties") if isinstance(schema, dict) else None
        title_names = [
            name for name, prop in (properties or {}).items()
            if isinstance(prop, dict) and prop.get("type") == "title"
        ]
        if len(title_names) != 1:
            raise GradeBlocked(
                "\u300c\u77e5\u8bc6\u70b9\u4e0e\u9519\u9898\u300d\u6570\u636e\u5e93\u5fc5\u987b\u6709\u4e14\u4ec5\u6709\u4e00\u4e2a\u6807\u9898\u5c5e\u6027\uff0c\u8bf7\u4fee\u6b63 Notion \u8bbe\u7f6e\u540e\u91cd\u8bd5\u3002"
            )
        self._wrong_answer_database = identity
        self._wrong_answer_title_property = title_names[0]

    def _wrong_answer_target(self):
        if self._wrong_answer_database is None or not self._wrong_answer_title_property:
            self.preflight_wrong_answer_database()
        return self._wrong_answer_database, self._wrong_answer_title_property

    def read_for_grading(self, target: GradeTarget) -> GradingInput:
        with get_client(self.settings) as client:
            blocks = client.list_children(target.page_id)
        prompt, unanswered = _grading_prompt(target, blocks)
        marker = _find_marker(blocks)
        strict_contract = _uses_e2e_grade_contract(target)
        provenance, fingerprint = _answer_provenance(blocks)
        return GradingInput(
            prompt=prompt,
            unanswered=unanswered,
            marker_present=marker is not None,
            existing_result=_existing_result(blocks, marker, target),
            answer_fingerprint=fingerprint,
            answer_provenance=provenance,
            result_marker=(
                _plain(marker) if marker is not None
                else GRADE_MARKERS[1] if strict_contract else GRADE_MARKERS[0]
            ),
            result_contract=GRADE_RESULT_CONTRACT_VERSION if strict_contract else None,
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

    def ensure_result(self, target: GradeTarget, *, operation_id: str, content: str,
                      score: float, graded_at: str, regrade: bool,
                      result_marker: str = GRADE_MARKERS[0],
                      append_plan: list[dict[str, Any]] | None = None) -> None:
        """Reconcile one immutable operation envelope against actual blocks.

        Older callers without a durably frozen plan retain the conservative
        partial-write behavior. Durable grading always supplies ``append_plan``.
        """
        rendered = markdown_to_blocks(
            _operation_result_markdown(operation_id, content, score=score, graded_at=graded_at,
                                       marker=result_marker)
        )
        if append_plan is None:
            with get_client(self.settings) as client:
                existing = client.list_children(target.page_id)
                if _find_complete_operation(existing, operation_id, content=content, score=score,
                                            graded_at=graded_at, marker=result_marker) is not None:
                    return
                if _find_operation(existing, operation_id) is not None:
                    raise GradeBlocked("\u6279\u6539\u7ed3\u679c\u5199\u5165\u4e0d\u5b8c\u6574\uff0c\u4e3a\u907f\u514d\u91cd\u590d\u5199\u5165\uff0c\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u5904\u7406\u3002")
                _append_and_verify(client, target.page_id, rendered, rendered)
            return
        plan = _validated_append_plan(append_plan, rendered)
        with get_client(self.settings) as client:
            existing = client.list_children(target.page_id)
            action, owned = _result_append_action(
                existing, plan, operation_id=operation_id, marker=result_marker, regrade=regrade
            )
            if action == "complete":
                return
            if action == "blocked":
                raise GradeBlocked("\u6279\u6539\u7ed3\u679c\u5199\u5165\u4e0d\u5b8c\u6574\uff0c\u5f52\u5c5e\u65e0\u6cd5\u5b89\u5168\u786e\u8ba4\uff0c\u672a\u4fee\u6539 Notion\u3002")
            if action == "replace-prefix":
                # Retire the marker first, then metadata from the end, and
                # the operation token last. Any interrupted transition retains
                # the durable token as unambiguous ownership evidence.
                retirement_order = (
                    list(reversed(owned))
                    if _plain(owned[0]).startswith("PKU_GRADE_OPERATION:")
                    else [owned[0], *reversed(owned[1:])]
                )
                for block in retirement_order:
                    block_id = str(block.get("id") or "")
                    if not block_id:
                        raise GradeBlocked("\u6279\u6539\u7ed3\u679c\u5199\u5165\u4e0d\u5b8c\u6574\uff0c\u5f52\u5c5e\u65e0\u6cd5\u5b89\u5168\u786e\u8ba4\uff0c\u672a\u4fee\u6539 Notion\u3002")
                    _archive_block_reconciled(client, target.page_id, block_id)
                _append_and_verify(client, target.page_id, plan, plan)
                return
            _append_and_verify(client, target.page_id, plan, plan)

    def verify_result(self, target: GradeTarget, *, operation_id: str, content: str,
                      score: float, graded_at: str, result_marker: str) -> bool:
        with get_client(self.settings) as client:
            blocks = client.list_children(target.page_id)
        return _find_complete_operation(
            blocks, operation_id, content=content, score=score,
            graded_at=graded_at, marker=result_marker,
        ) is not None

    def result_cleanup_plan(self, target: GradeTarget, *, operation_id: str,
                            content: str | None = None, score: float | None = None,
                            graded_at: str = "", result_marker: str = GRADE_MARKERS[0]) -> list[str]:
        """Return the exact stale app-owned block identities to retire."""
        with get_client(self.settings) as client:
            existing = client.list_children(target.page_id)
        current = (
            _find_complete_operation(existing, operation_id, content=content, score=score,
                                     graded_at=graded_at, marker=result_marker)
            if content is not None and score is not None else _find_operation(existing, operation_id)
        )
        if current is None:
            raise GradeBlocked("\u6279\u6539\u7ed3\u679c\u6807\u8bb0\u5df2\u5931\u6548\uff0c\u8bf7\u5237\u65b0\u540e\u91cd\u8bd5\u3002")
        return [str(block["id"]) for block in _stale_result_blocks(existing, current) if block.get("id")]

    def archive_result_block(self, target: GradeTarget, *, block_id: str) -> None:
        """Archive one planned block, reconciling an ambiguous response by reread."""
        with get_client(self.settings) as client:
            if not any(str(block.get("id") or "") == block_id for block in client.list_children(target.page_id)):
                return
            _archive_block_reconciled(client, target.page_id, block_id)

    def cleanup_result(self, target: GradeTarget, *, operation_id: str, **_kwargs) -> None:
        """Compatibility path for callers without a durable cleanup journal."""
        for block_id in self.result_cleanup_plan(target, operation_id=operation_id):
            self.archive_result_block(target, block_id=block_id)

    def ensure_wrong_answer(self, target: GradeTarget, question: dict[str, Any], *,
                            key: str, operation_id: str) -> None:
        if self.directory_service is None:
            raise GradeBlocked("\u9519\u9898\u672c\u672a\u914d\u7f6e\uff0c\u65e0\u6cd5\u5b8c\u6210\u9519\u9898\u767b\u8bb0\u3002")
        identity, title_property = self._wrong_answer_target()
        if self.verify_wrong_answer(target, question, key=key, operation_id=operation_id):
            return
        title = _wrong_answer_row_title(target, question, key)
        with get_client(self.settings) as client:
            try:
                client.create_database_row(
                    identity.id, {title_property: prop_title(title)}, retry=False
                )
            except Exception:
                if not self.verify_wrong_answer(
                    target, question, key=key, operation_id=operation_id
                ):
                    raise
        if not self.verify_wrong_answer(
            target, question, key=key, operation_id=operation_id
        ):
            raise GradeBlocked("\u9519\u9898\u767b\u8bb0\u5199\u5165\u540e\u672a\u80fd\u786e\u8ba4\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")

    def verify_wrong_answer(self, target: GradeTarget, question: dict[str, Any], *,
                            key: str, operation_id: str) -> bool:
        if self.directory_service is None:
            return False
        identity, _title_property = self._wrong_answer_target()
        title = _wrong_answer_row_title(target, question, key)
        with get_client(self.settings) as client:
            rows = client.query_database(
                identity.id,
                filter={"property": self._wrong_answer_title_property,
                        "title": {"equals": title}},
                page_size=2,
                max_results=2,
            )
        if len(rows) > 1:
            raise GradeBlocked(
                "\u9519\u9898\u767b\u8bb0\u952e\u5b58\u5728\u91cd\u590d\u884c\uff0c\u65e0\u6cd5\u5b89\u5168\u7ee7\u7eed\uff0c\u8bf7\u68c0\u67e5 Notion \u6570\u636e\u5e93\u3002"
            )
        return len(rows) == 1

    def wrong_answer_exists(self, target: GradeTarget, question: dict[str, Any]) -> bool:
        key = str(question.get("wrong_answer_key") or question.get("number") or "")
        if not key:
            return False
        return self.verify_wrong_answer(
            target, question, key=key, operation_id="legacy"
        )

    def create_wrong_answer(self, target: GradeTarget, question: dict[str, Any]) -> None:
        key = str(question.get("wrong_answer_key") or question.get("number") or "")
        if not key:
            raise GradeBlocked("\u9519\u9898\u7f3a\u5c11\u53ef\u8bc6\u522b\u7684\u9898\u53f7\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")
        self.ensure_wrong_answer(
            target, question, key=key, operation_id="legacy"
        )


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


class ExerciseGrader:
    def __init__(self, *, directory_service, relay, page_adapter, clock=None):
        self.directory_service = directory_service
        self.relay = relay
        self.page_adapter = page_adapter
        self.clock = clock or _graded_now
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}

    def prepare(self, exercise_id: str) -> tuple[GradeTarget, GradingInput]:
        target = self.directory_service.exercise_grade_target(exercise_id)
        if target is None:
            raise GradeBlocked("\u7ec3\u4e60\u9875\u6620\u5c04\u7f3a\u5931\uff0c\u65e0\u6cd5\u786e\u5b9a\u6279\u6539\u76ee\u6807\u3002")
        grade_target = GradeTarget(operation="grade", page_id=target["page_id"], page_url=target["page_url"], title=target["title"], course_id=target["course_id"], course_title=target["course_title"], scope=target["scope"])
        pending = self._pending.get(grade_target.page_id) or self._record(grade_target.page_id)
        phase = pending.get("phase") if isinstance(pending, dict) else None
        recovering = isinstance(pending, dict) and (
            phase in _NONTERMINAL_PHASES or pending.get("status") == "pending"
        )
        # Fresh work and a durable, not-yet-charged intent retain the strict
        # dependency gate. Post-charge phases enter recovery first and check
        # the dependency only immediately before wrong-answer access.
        if not recovering or phase == PHASE_INTENT:
            preflight = getattr(self.page_adapter, "preflight_wrong_answer_database", None)
            if preflight is not None:
                preflight()
        verify_parent = getattr(self.page_adapter, "verify_parent", None)
        if verify_parent is not None:
            verify_parent(grade_target)
        grading_input = self.page_adapter.read_for_grading(grade_target)
        if not recovering and grading_input.unanswered and not grading_input.marker_present:
            raise GradeBlocked(INCOMPLETE_ANSWERS_REASON, unanswered=grading_input.unanswered)
        return grade_target, grading_input

    def has_pending(self, exercise_id: str) -> bool:
        record = self._record(exercise_id)
        return isinstance(record, dict) and (
            record.get("phase") in _NONTERMINAL_PHASES or record.get("status") == "pending"
        )

    def grade(self, prepared: tuple[GradeTarget, GradingInput], *, force: bool = False) -> dict[str, Any]:
        target, grading_input = prepared
        with self._lock:
            pending = self._pending.get(target.page_id) or self._record(target.page_id)
            if isinstance(pending, dict) and (
                pending.get("phase") in _NONTERMINAL_PHASES or pending.get("status") == "pending"
            ):
                return self._resume(target, grading_input, dict(pending))

            local_record = self._record(target.page_id)
            previous_fingerprint = local_record.get("answer_fingerprint") if isinstance(local_record, dict) else ""
            provenance = _resolved_provenance(grading_input)
            fingerprints_equal = bool(
                provenance == ANSWER_PROVENANCE_KNOWN
                and grading_input.answer_fingerprint and previous_fingerprint
                and grading_input.answer_fingerprint == previous_fingerprint
            )
            if grading_input.marker_present:
                if provenance == ANSWER_PROVENANCE_MARKER_ONLY:
                    return _reuse_result(grading_input.existing_result, target, settlement_known=_has_known_settlement(local_record))
                if fingerprints_equal:
                    return _reuse_result(grading_input.existing_result, target, settlement_known=True)
                if not force:
                    raise GradeBlocked("\u65e0\u6cd5\u786e\u8ba4\u7b54\u6848\u662f\u5426\u672a\u53d8\uff0c\u8bf7\u786e\u8ba4\u91cd\u65b0\u6279\u6539\u3002")

            operation_id = uuid.uuid4().hex
            intent = {
                "version": 2, "status": "pending", "phase": PHASE_INTENT,
                "operation_id": operation_id, "operation": "grade",
                "page_id": target.page_id, "page_url": target.page_url,
                "course_id": target.course_id, "course_title": target.course_title,
                "title": target.title, "scope": target.scope,
                "answer_fingerprint": grading_input.answer_fingerprint,
                "answer_provenance": provenance, "regrade": bool(grading_input.marker_present),
                "result_marker": grading_input.result_marker if grading_input.result_marker in GRADE_MARKERS else GRADE_MARKERS[0],
                "result_contract": grading_input.result_contract,
            }
            # A configured record store is the durable boundary. Legacy
            # injected test/services without one keep their in-memory behavior.
            durable = getattr(self.directory_service, "grading_record_store", None) is not None
            if durable:
                # Intent failure is pre-charge. Do not invoke the relay.
                self._persist(target.page_id, intent)
            self._pending[target.page_id] = dict(intent)
            quota = self.relay.quota()
            before = quota.get("llm_points_remaining") if quota.get("available") else None
            started = dict(intent, phase=PHASE_RELAY_STARTED)
            if durable:
                self._persist(target.page_id, started)
            self._pending[target.page_id] = dict(started)
            try:
                result = self.relay.grade(grading_input.prompt)
            except Exception as exc:
                if _definitive_relay_failure(exc):
                    if durable:
                        self._persist(target.page_id, intent)
                    self._pending[target.page_id] = dict(intent)
                raise
            charge = result.get("points_charged")
            if not isinstance(charge, (int, float)):
                if durable:
                    self._persist(target.page_id, intent)
                self._pending[target.page_id] = dict(intent)
                raise GradeBlocked("\u4e91\u7aef\u6ca1\u6709\u8fd4\u56de\u6709\u6548\u7684\u7528\u91cf\u7ed3\u7b97\uff0c\u8bf7\u91cd\u8bd5\u3002")
            remaining = float(before) - float(charge) if isinstance(before, (int, float)) else None
            charged = dict(
                started, phase=PHASE_RELAY_SUCCEEDED, content=result.get("content"),
                points_charged=float(charge), points_remaining=remaining,
                result_page_url=target.page_url, graded_at=self.clock(),
            )
            self._pending[target.page_id] = dict(charged)
            try:
                # The relay response and authoritative settlement become durable
                # before parsing or any Notion mutation.
                self._persist(target.page_id, charged)
                return self._resume(target, grading_input, charged)
            except GradeSettlementError:
                raise
            except GradeBlocked as exc:
                raise GradeSettlementError(exc.reason, points_charged=float(charge), points_remaining=_authoritative_balance(self.relay, remaining, 0)) from exc
            except Exception as exc:
                raise GradeSettlementError("\u6279\u6539\u7ed3\u679c\u5199\u56de Notion \u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002", points_charged=float(charge), points_remaining=_authoritative_balance(self.relay, remaining, 0)) from exc

    def _invoke_relay_from_intent(self, target: GradeTarget, grading_input: GradingInput,
                                  intent: dict[str, Any]) -> dict[str, Any]:
        quota = self.relay.quota()
        before = quota.get("llm_points_remaining") if quota.get("available") else None
        started = dict(intent, phase=PHASE_RELAY_STARTED)
        self._persist(target.page_id, started)
        self._pending[target.page_id] = dict(started)
        try:
            result = self.relay.grade(grading_input.prompt)
        except Exception as exc:
            if _definitive_relay_failure(exc):
                self._persist(target.page_id, intent)
                self._pending[target.page_id] = dict(intent)
            raise
        charge = result.get("points_charged")
        if not isinstance(charge, (int, float)):
            self._persist(target.page_id, intent)
            self._pending[target.page_id] = dict(intent)
            raise GradeBlocked("\u4e91\u7aef\u6ca1\u6709\u8fd4\u56de\u6709\u6548\u7684\u7528\u91cf\u7ed3\u7b97\uff0c\u8bf7\u91cd\u8bd5\u3002")
        remaining = float(before) - float(charge) if isinstance(before, (int, float)) else None
        charged = dict(started, phase=PHASE_RELAY_SUCCEEDED,
                       content=result.get("content"), points_charged=float(charge),
                       points_remaining=remaining, result_page_url=target.page_url,
                       graded_at=self.clock())
        self._persist(target.page_id, charged)
        self._pending[target.page_id] = dict(charged)
        return self._resume(target, grading_input, charged)

    def _record(self, exercise_id: str) -> dict[str, Any] | None:
        records = getattr(self.directory_service, "local_grading_records", {})
        value = records.get(exercise_id) if isinstance(records, dict) else None
        return dict(value) if isinstance(value, dict) else None

    def _persist(self, exercise_id: str, record: dict[str, Any]) -> None:
        persist = getattr(self.directory_service, "record_grading", None)
        if persist is not None:
            persist(exercise_id, record)

    def _resume(self, target: GradeTarget, grading_input: GradingInput, record: dict[str, Any]) -> dict[str, Any]:
        phase = record.get("phase")
        if not phase and record.get("status") == "pending":
            phase = PHASE_RELAY_SUCCEEDED
            record.update(phase=phase, operation_id=record.get("operation_id") or uuid.uuid4().hex,
                          regrade=bool(grading_input.marker_present))
        if phase == PHASE_INTENT:
            return self._invoke_relay_from_intent(target, grading_input, record)
        if phase == PHASE_RELAY_STARTED:
            # No settlement is knowable in the relay-commit/client-crash
            # interval. Do not replay or fabricate zero-charge settlement.
            raise GradeBlocked("\u65e0\u6cd5\u786e\u8ba4\u4e91\u7aef\u8bf7\u6c42\u662f\u5426\u5df2\u7ed3\u7b97\uff0c\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u6838\u5bf9\u540e\u518d\u91cd\u8bd5\u3002")
        charge = float(record.get("points_charged", 0))
        remaining = record.get("points_remaining")
        try:
            content = record.get("content")
            score = record.get("score")
            if phase == PHASE_RELAY_SUCCEEDED:
                response_contract = _result_contract_version(content)
                if record.get("result_contract") == GRADE_RESULT_CONTRACT_VERSION or response_contract is not None:
                    validated = _validate_grade_result(content)
                    content = json.dumps(validated, ensure_ascii=False, separators=(",", ":"))
                    record["content"] = content
                    score = validated["score"]
                else:
                    score = _score(content)
                record["score"] = score
                if "append_plan" not in record:
                    record["append_plan"] = markdown_to_blocks(
                        _operation_result_markdown(
                            record["operation_id"], record["content"], score=score,
                            graded_at=record["graded_at"],
                            marker=record.get("result_marker") or GRADE_MARKERS[0],
                        )
                    )
                    self._persist(target.page_id, record)
                    self._pending[target.page_id] = copy.deepcopy(record)
                self._ensure_result(target, record)
                record["phase"] = PHASE_RESULT_VERIFIED
                self._persist(target.page_id, record)
                self._pending[target.page_id] = dict(record)
                phase = PHASE_RESULT_VERIFIED

            if phase == PHASE_RESULT_VERIFIED:
                self._verify_current_result(target, record)
                self._cleanup_result(target, record)
                record["phase"] = PHASE_RESULT_RECONCILED
                self._persist(target.page_id, record)
                self._pending[target.page_id] = dict(record)
                phase = PHASE_RESULT_RECONCILED

            if phase == PHASE_RESULT_RECONCILED:
                self._verify_current_result(target, record)
                self._preflight_wrong_answers_if_required(record)
                self._reconcile_wrong_answers(target, record)
                record["phase"] = PHASE_WRONG_ANSWERS_VERIFIED
                self._persist(target.page_id, record)
                self._pending[target.page_id] = dict(record)
                phase = PHASE_WRONG_ANSWERS_VERIFIED

            if phase == PHASE_WRONG_ANSWERS_VERIFIED:
                self._verify_current_result(target, record)
                self._preflight_wrong_answers_if_required(record)
                self._verify_all_wrong_answers(target, record)
                transient = {"content", "append_plan", "wrong_answer_progress", "cleanup_block_ids", "cleanup_progress"}
                completed = {key: value for key, value in record.items() if key not in transient}
                completed.update(status="graded", marker_present=True, phase=PHASE_COMPLETED)
                self._persist(target.page_id, completed)
                self._pending.pop(target.page_id, None)
                return _summary(target, completed)
            if phase == PHASE_COMPLETED:
                return _summary(target, record)
            raise GradeBlocked("\u6279\u6539\u6062\u590d\u72b6\u6001\u65e0\u6548\uff0c\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u3002")
        except GradeSettlementError:
            raise
        except GradeBlocked as exc:
            raise GradeSettlementError(exc.reason, points_charged=charge, points_remaining=remaining) from exc
        except Exception as exc:
            raise GradeSettlementError("\u6279\u6539\u7ed3\u679c\u5199\u56de Notion \u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002", points_charged=charge, points_remaining=remaining) from exc

    def _ensure_result(self, target: GradeTarget, record: dict[str, Any]) -> None:
        ensure = getattr(self.page_adapter, "ensure_result", None)
        if ensure is not None:
            kwargs = {
                "operation_id": record["operation_id"], "content": record["content"],
                "score": record["score"], "graded_at": record["graded_at"],
                "regrade": bool(record.get("regrade")),
                "result_marker": record.get("result_marker") or GRADE_MARKERS[0],
            }
            if record.get("append_plan") is not None:
                kwargs["append_plan"] = record["append_plan"]
            ensure(target, **kwargs)
            return
        writer = self.page_adapter.update_result if record.get("regrade") else self.page_adapter.write_result
        writer(target, content=record["content"], score=record["score"], graded_at=record["graded_at"])

    def _verify_current_result(self, target: GradeTarget, record: dict[str, Any]) -> None:
        verify = getattr(self.page_adapter, "verify_result", None)
        if verify is None:
            return
        if not verify(target, operation_id=record["operation_id"], content=record["content"],
                      score=record["score"], graded_at=record["graded_at"],
                      result_marker=record.get("result_marker") or GRADE_MARKERS[0]):
            raise GradeBlocked("\u6279\u6539\u7ed3\u679c\u4e0d\u5b8c\u6574\uff0c\u65e0\u6cd5\u7ee7\u7eed\u6062\u590d\u3002")

    def _verify_all_wrong_answers(self, target: GradeTarget, record: dict[str, Any]) -> None:
        verify = getattr(self.page_adapter, "verify_wrong_answer", None)
        if verify is None:
            return
        rows = [row for row in _parse_grade_results(record.get("content")) if row.get("register_wrong")]
        for row in rows:
            key = _wrong_answer_operation_key(record["operation_id"], row)
            if not verify(target, row, key=key, operation_id=record["operation_id"]):
                raise GradeBlocked("\u9519\u9898\u767b\u8bb0\u672a\u80fd\u786e\u8ba4\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")

    def _preflight_wrong_answers_if_required(self, record: dict[str, Any]) -> None:
        rows = [row for row in _parse_grade_results(record.get("content")) if row.get("register_wrong")]
        if not rows:
            return
        preflight = getattr(self.page_adapter, "preflight_wrong_answer_database", None)
        if preflight is not None:
            preflight()

    def _cleanup_result(self, target: GradeTarget, record: dict[str, Any]) -> None:
        planner = getattr(self.page_adapter, "result_cleanup_plan", None)
        archiver = getattr(self.page_adapter, "archive_result_block", None)
        if planner is not None and archiver is not None:
            if "cleanup_block_ids" not in record:
                record["cleanup_block_ids"] = list(
                    planner(target, operation_id=record["operation_id"], content=record["content"],
                            score=record["score"], graded_at=record["graded_at"],
                            result_marker=record.get("result_marker") or GRADE_MARKERS[0])
                )
                record["cleanup_progress"] = []
                self._persist(target.page_id, record)
                self._pending[target.page_id] = dict(record)
            progress = list(record.get("cleanup_progress") or [])
            for block_id in record["cleanup_block_ids"]:
                # The archiver rereads actual Notion state even for journaled
                # progress, so local state never substitutes for reconciliation.
                archiver(target, block_id=block_id)
                if block_id not in progress:
                    progress.append(block_id)
                    record["cleanup_progress"] = list(progress)
                    self._persist(target.page_id, record)
                    self._pending[target.page_id] = dict(record)
            remaining = list(
                planner(target, operation_id=record["operation_id"], content=record["content"],
                        score=record["score"], graded_at=record["graded_at"],
                        result_marker=record.get("result_marker") or GRADE_MARKERS[0])
            )
            if remaining:
                planned = list(record["cleanup_block_ids"])
                changed = False
                for block_id in remaining:
                    if block_id not in planned:
                        planned.append(block_id)
                        changed = True
                if changed:
                    record["cleanup_block_ids"] = planned
                    self._persist(target.page_id, record)
                    self._pending[target.page_id] = dict(record)
                raise GradeBlocked("旧批改结果尚未完全清理，请稍后重试。")
            return
        cleanup = getattr(self.page_adapter, "cleanup_result", None)
        if cleanup is not None:
            cleanup(target, operation_id=record["operation_id"], content=record["content"],
                    score=record["score"], graded_at=record["graded_at"])

    def _reconcile_wrong_answers(self, target: GradeTarget, record: dict[str, Any]) -> None:
        rows = [row for row in _parse_grade_results(record.get("content")) if row.get("register_wrong")]
        ensure = getattr(self.page_adapter, "ensure_wrong_answer", None)
        verify = getattr(self.page_adapter, "verify_wrong_answer", None)
        if ensure is not None and verify is not None:
            progress = list(record.get("wrong_answer_progress") or [])
            for row in rows:
                key = _wrong_answer_operation_key(record["operation_id"], row)
                if not verify(target, row, key=key, operation_id=record["operation_id"]):
                    ensure(target, row, key=key, operation_id=record["operation_id"])
                if not verify(target, row, key=key, operation_id=record["operation_id"]):
                    raise GradeBlocked("\u9519\u9898\u767b\u8bb0\u672a\u80fd\u786e\u8ba4\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")
                if key not in progress:
                    progress.append(key)
                    record["wrong_answer_progress"] = list(progress)
                    self._persist(target.page_id, record)
                    self._pending[target.page_id] = dict(record)
            return
        exists = getattr(self.page_adapter, "wrong_answer_exists", None)
        create = getattr(self.page_adapter, "create_wrong_answer", None)
        if exists is not None and create is not None:
            for row in rows:
                if not exists(target, row):
                    create(target, row)


def _wrong_answer_row_title(
    target: GradeTarget, question: dict[str, Any], key: str
) -> str:
    return f"{target.title} \u7b2c{question.get('number')}\u9898 \u00b7 {key}"


def _wrong_answer_operation_key(operation_id: str, question: dict[str, Any]) -> str:
    logical = str(question.get("wrong_answer_key") or question.get("number") or "").strip()
    if not logical:
        raise GradeBlocked("\u9519\u9898\u7f3a\u5c11\u53ef\u8bc6\u522b\u7684\u9898\u53f7\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")
    return f"{operation_id}:{logical}"


def _summary(target: GradeTarget, record: dict[str, Any]) -> dict[str, Any]:
    return {"status": "completed", "exercise_id": target.page_id, "title": target.title,
            "score": record.get("score"), "graded_at": record.get("graded_at", ""),
            "points_charged": float(record.get("points_charged", 0)),
            "points_remaining": record.get("points_remaining"),
            "result_page_url": record.get("result_page_url") or target.page_url}


def _graded_now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds")


def _definitive_relay_failure(exc: Exception) -> bool:
    """Only an explicit HTTP response proves the request did not commit."""
    return getattr(exc, "status_code", None) in {401, 402, 502, 503}


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


def _result_contract_version(content: Any) -> str | None:
    try:
        # Exactly one fenced wrapper around the contract response is
        # tolerated so the version stays detectable; anything else is not.
        value = (
            json.loads(unwrap_single_json_fence(content))
            if isinstance(content, str)
            else content
        )
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    version = value.get("contract_version")
    return version if isinstance(version, str) else None


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
    number = float(value)
    if not math.isfinite(number):
        raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
    return number


def _clean_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _validate_grade_result(content: Any) -> dict[str, Any]:
    """Validate and normalize the complete adopted five-question response."""
    try:
        # Exactly one fenced wrapper around an otherwise-contract-valid
        # response is unwrapped here; all validation below is unchanged.
        value = (
            json.loads(unwrap_single_json_fence(content))
            if isinstance(content, str)
            else content
        )
    except (TypeError, ValueError) as exc:
        raise GradeBlocked(GRADE_RESULT_INVALID_REASON) from exc
    if not isinstance(value, dict) or value.get("contract_version") != GRADE_RESULT_CONTRACT_VERSION:
        raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
    rows = value.get("questions")
    if not isinstance(rows, list) or len(rows) != len(E2E_GRADE_QUESTION_RUBRIC):
        raise GradeBlocked(GRADE_RESULT_INVALID_REASON)

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for expected, row in zip(E2E_GRADE_QUESTION_RUBRIC, rows):
        if not isinstance(row, dict):
            raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
        question_id = row.get("question_id")
        if not isinstance(question_id, str) or question_id in seen or question_id != expected.question_id:
            raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
        seen.add(question_id)
        if row.get("number") != expected.number or row.get("type") != expected.type:
            raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
        if row.get("wrong_answer_key") != question_id:
            raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
        score = _finite_number(row.get("score"))
        maximum = _finite_number(row.get("max_score"))
        if maximum != float(expected.max_score) or not 0 <= score <= maximum:
            raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
        if score != float(expected.expected_score):
            raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
        outcome = row.get("outcome")
        register_wrong = row.get("register_wrong")
        if outcome != expected.expected_outcome or not isinstance(register_wrong, bool):
            raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
        if register_wrong is not expected.register_wrong:
            raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
        feedback = row.get("feedback")
        if not isinstance(feedback, str) or not feedback.strip():
            raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
        normalized.append({
            "question_id": question_id,
            "number": expected.number,
            "type": expected.type,
            "score": _clean_number(score),
            "max_score": _clean_number(maximum),
            "outcome": outcome,
            "partial": 0 < score < maximum,
            "register_wrong": register_wrong,
            "wrong_answer_key": question_id,
            "feedback": feedback.strip(),
        })

    earned = _finite_number(value.get("earned_points"))
    maximum = _finite_number(value.get("max_points"))
    expected_earned = sum(float(row["score"]) for row in normalized)
    expected_maximum = sum(float(row["max_score"]) for row in normalized)
    if earned != expected_earned or maximum != expected_maximum or maximum <= 0:
        raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
    normalized_score = round(earned / maximum * 100, 6)
    score = _finite_number(value.get("score"))
    if score != normalized_score or not 0 <= score <= 100:
        raise GradeBlocked(GRADE_RESULT_INVALID_REASON)
    return {
        "contract_version": GRADE_RESULT_CONTRACT_VERSION,
        "score": _clean_number(score),
        "earned_points": _clean_number(earned),
        "max_points": _clean_number(maximum),
        "questions": normalized,
    }


def _parse_grade_results(content: Any) -> list[dict[str, Any]]:
    """Normalize per-question results while keeping them out of panel payloads."""
    try:
        value = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError) as exc:
        raise GradeBlocked("AI 返回的批改结果不完整，请重试。") from exc
    if isinstance(value, dict) and value.get("contract_version") == GRADE_RESULT_CONTRACT_VERSION:
        return _validate_grade_result(value)["questions"]
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


def _uses_e2e_grade_contract(target: GradeTarget) -> bool:
    return target.title.lstrip().startswith("[E2E]")


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
    payload = {
        "operation": "grade",
        "target": {
            "page_id": target.page_id, "page_url": target.page_url,
            "course_id": target.course_id, "course_title": target.course_title,
            "scope": target.scope,
        },
        "blocks": blocks,
    }
    if _uses_e2e_grade_contract(target):
        payload["response_contract"] = {
            "version": GRADE_RESULT_CONTRACT_VERSION,
            "format": "Return one JSON object only. Do not omit, add, or duplicate questions.",
            "required_top_level_fields": [
                "contract_version", "score", "earned_points", "max_points", "questions"
            ],
            "required_question_fields": [
                "question_id", "number", "type", "score", "max_score",
                "outcome", "register_wrong", "wrong_answer_key", "feedback",
            ],
            "questions": [item.provider_schema() for item in E2E_GRADE_QUESTION_RUBRIC],
            "score_formula": "earned_points / max_points * 100",
            "wrong_answer_question_ids": ["Q2", "Q5"],
        }
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


def _operation_token(operation_id: str) -> str:
    return f"PKU_GRADE_OPERATION:{operation_id}"


def _operation_result_markdown(operation_id: str, content: str, *, score: float,
                               graded_at: str, marker: str = GRADE_MARKERS[0]) -> str:
    safe_marker = marker if marker in GRADE_MARKERS else GRADE_MARKERS[0]
    return f"## {safe_marker}\n\n{_operation_token(operation_id)}\n\n\u6279\u6539\u65f6\u95f4\uff1a{graded_at}\n\n\u603b\u5206\uff1a{score}\n\n```json\n{content}\n```"


def _block_signature(block: dict[str, Any]) -> tuple[str, str]:
    """Normalize request and Notion response blocks to owned content."""
    kind = str(block.get("type") or "")
    if kind not in {"heading_2", "paragraph", "code"}:
        return "", ""
    return kind, _plain(block)


def _blocks_equal(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return _block_signature(actual) == _block_signature(expected)


def _validated_append_plan(
    stored: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(stored, list) or not stored or len(stored) != len(expected):
        raise GradeBlocked("\u6279\u6539\u7ed3\u679c\u5199\u5165\u8ba1\u5212\u65e0\u6548\uff0c\u672a\u4fee\u6539 Notion\u3002")
    if any(not isinstance(block, dict) for block in stored):
        raise GradeBlocked("\u6279\u6539\u7ed3\u679c\u5199\u5165\u8ba1\u5212\u65e0\u6548\uff0c\u672a\u4fee\u6539 Notion\u3002")
    if any(not _blocks_equal(left, right) for left, right in zip(stored, expected)):
        raise GradeBlocked("\u6279\u6539\u7ed3\u679c\u5199\u5165\u8ba1\u5212\u4e0d\u5339\u914d\uff0c\u672a\u4fee\u6539 Notion\u3002")
    return copy.deepcopy(stored)


def _looks_app_owned_result_block(block: dict[str, Any]) -> bool:
    text = _plain(block)
    return (
        text in GRADE_MARKERS
        or text.startswith("PKU_GRADE_OPERATION:")
        or text.startswith("\u6279\u6539\u65f6\u95f4\uff1a")
        or text.startswith("\u603b\u5206\uff1a")
        or block.get("type") == "code"
    )


def _operation_token_indexes(blocks: list[dict], operation_id: str) -> list[int]:
    token = _operation_token(operation_id)
    return [index for index, block in enumerate(blocks) if _plain(block) == token]


def _complete_owned_envelope_at(blocks: list[dict], marker_index: int) -> bool:
    following = blocks[marker_index + 1:marker_index + 5]
    return len(following) == 4 and (
        _plain(following[0]).startswith("PKU_GRADE_OPERATION:")
        and _plain(following[1]).startswith("\u6279\u6539\u65f6\u95f4\uff1a")
        and _plain(following[2]).startswith("\u603b\u5206\uff1a")
        and following[3].get("type") == "code"
    )


def _result_append_action(
    blocks: list[dict], plan: list[dict], *, operation_id: str, marker: str,
    regrade: bool = False,
) -> tuple[str, list[dict]]:
    """Classify exact ownership without inferring from a marker alone."""
    token_indexes = _operation_token_indexes(blocks, operation_id)
    if len(token_indexes) > 1:
        return "blocked", []
    if not token_indexes:
        markers = [index for index, block in enumerate(blocks) if _plain(block) == marker]
        if not markers:
            return "append", []
        if regrade and all(_complete_owned_envelope_at(blocks, index) for index in markers):
            return "append", []
        return "blocked", []
    token_index = token_indexes[0]
    start = token_index - 1
    if start < 0 or not _blocks_equal(blocks[start], plan[0]):
        # Interrupted marker-first retirement can leave the exact token after
        # realistic leading answer blocks. Those blocks are never owned.
        owned = []
        for offset, expected in enumerate(plan[1:]):
            index = token_index + offset
            if index >= len(blocks):
                break
            actual = blocks[index]
            if not _blocks_equal(actual, expected):
                if _looks_app_owned_result_block(actual):
                    return "blocked", []
                break
            owned.append(actual)
        return ("replace-prefix", owned) if owned else ("blocked", [])
    owned: list[dict] = []
    for offset, expected in enumerate(plan):
        index = start + offset
        if index >= len(blocks):
            break
        actual = blocks[index]
        if not _blocks_equal(actual, expected):
            if _looks_app_owned_result_block(actual):
                return "blocked", []
            return ("replace-prefix", owned) if owned and index < len(blocks) else ("blocked", [])
        owned.append(actual)
    if len(owned) == len(plan):
        return "complete", owned
    return "replace-prefix", owned


def _archive_block_reconciled(client, page_id: str, block_id: str) -> None:
    error = None
    try:
        client.archive_block(block_id, retry=False)
    except Exception as exc:
        error = exc
    remaining = client.list_children(page_id)
    if any(str(block.get("id") or "") == block_id for block in remaining):
        if error is not None:
            raise error
        raise GradeBlocked("批改结果归档后未能确认，请稍后重试。")

def _append_and_verify(client, page_id: str, suffix: list[dict], plan: list[dict]) -> None:
    try:
        if suffix:
            client.append_blocks(page_id, suffix, retry=False)
    except Exception:
        reread = client.list_children(page_id)
        if not _contains_exact_plan(reread, plan):
            raise
        return
    verified = client.list_children(page_id)
    if not _contains_exact_plan(verified, plan):
        raise GradeBlocked("\u6279\u6539\u7ed3\u679c\u5199\u5165\u540e\u672a\u80fd\u786e\u8ba4\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")


def _contains_exact_plan(blocks: list[dict], plan: list[dict]) -> bool:
    if not plan:
        return False
    for start in range(0, len(blocks) - len(plan) + 1):
        if all(_blocks_equal(blocks[start + offset], expected) for offset, expected in enumerate(plan)):
            return True
    return False


def _find_operation(blocks: list[dict], operation_id: str) -> dict | None:
    token = _operation_token(operation_id)
    return next((block for block in blocks if _plain(block) == token), None)


def _find_complete_operation(blocks: list[dict], operation_id: str, *, content: str,
                             score: float, graded_at: str, marker: str) -> dict | None:
    token = _find_operation(blocks, operation_id)
    if token is None:
        return None
    index = blocks.index(token)
    if index == 0 or _plain(blocks[index - 1]) != marker:
        return None
    expected = [f"\u6279\u6539\u65f6\u95f4\uff1a{graded_at}", f"\u603b\u5206\uff1a{score}"]
    if index + 3 >= len(blocks):
        return None
    if [_plain(blocks[index + 1]), _plain(blocks[index + 2])] != expected:
        return None
    code = blocks[index + 3]
    if code.get("type") != "code" or _plain(code) != str(content).strip():
        return None
    return token


def _stale_result_blocks(blocks: list[dict], current_operation: dict) -> list[dict]:
    """Return only structurally complete stale app-owned result envelopes."""
    current_index = blocks.index(current_operation)
    current_marker = next((index for index in range(current_index, -1, -1)
                           if _plain(blocks[index]) in GRADE_MARKERS), None)
    stale: list[dict] = []
    for index, marker in enumerate(blocks):
        if _plain(marker) not in GRADE_MARKERS or index == current_marker:
            continue
        following = blocks[index + 1:index + 5]
        # Durable envelopes are exactly marker, operation token, timestamp,
        # score, and JSON code. Never infer ownership from one matching block.
        if len(following) >= 4 and (
            _plain(following[0]).startswith("PKU_GRADE_OPERATION:")
            and _plain(following[1]).startswith("\u6279\u6539\u65f6\u95f4\uff1a")
            and _plain(following[2]).startswith("\u603b\u5206\uff1a")
            and following[3].get("type") == "code"
        ):
            stale.extend([marker, *following[:4]])
            continue
        # Legacy pre-operation envelopes are exactly marker, timestamp, score,
        # and JSON code. This permits safe migration without touching answers.
        legacy = blocks[index + 1:index + 4]
        if len(legacy) >= 3 and (
            _plain(legacy[0]).startswith("\u6279\u6539\u65f6\u95f4\uff1a")
            and _plain(legacy[1]).startswith("\u603b\u5206\uff1a")
            and legacy[2].get("type") == "code"
        ):
            stale.extend([marker, *legacy[:3]])
    return stale


def _result_markdown(content: str, *, score: float, graded_at: str) -> str:
    return f"## 批改结果\n\n批改时间：{graded_at}\n\n总分：{score}\n\n```json\n{content}\n```"


def make_grading_service(settings, directory_service, relay):
    return ExerciseGrader(directory_service=directory_service, relay=relay, page_adapter=RealGradingPageAdapter(settings, directory_service))


def make_fake_grading_service(directory_service, relay, *, fail_wrong_answer_preflight_once: bool = False):
    return ExerciseGrader(
        directory_service=directory_service,
        relay=relay,
        page_adapter=FakeGradingPageAdapter(
            fail_wrong_answer_preflight_once=fail_wrong_answer_preflight_once
        ),
    )


__all__ = ["PHASE_INTENT", "PHASE_RELAY_STARTED", "PHASE_RELAY_SUCCEEDED", "PHASE_RESULT_VERIFIED", "PHASE_RESULT_RECONCILED", "PHASE_WRONG_ANSWERS_VERIFIED", "PHASE_COMPLETED", "ANSWER_PROVENANCE_KNOWN", "ANSWER_PROVENANCE_MARKER_ONLY", "ANSWER_PROVENANCE_UNKNOWN", "GRADE_ESTIMATE_LABEL", "INCOMPLETE_ANSWERS_REASON", "GradeTarget", "GradingInput", "GradeBlocked", "RealGradingPageAdapter", "FakeGradingPageAdapter", "ExerciseGrader", "make_grading_service", "make_fake_grading_service", "_parse_grade_results"]
