"""Metered, idempotent exercise organization for the student panel.

Question material crosses only the local organizer, relay, and Notion adapter.
Panel responses and the directory retain metadata and settlement only.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..notion import get_client, markdown_to_blocks
from ..notion_meta import ExerciseEntity
from ..notion_meta.titles import is_exercise_title
from ..pipeline import collect_jobs, recording_stage

ESTIMATE_LABEL = "预计 1–5 AI 点 · 完成后按实际用量结算"
ESTIMATE_MIN_POINTS = 1
ESTIMATE_MAX_POINTS = 5
MAX_NOTE_CHARS = 12000
MAX_PROMPT_CHARS = 100000
_ALLOWED_TYPES = {"选择", "判断", "填空", "简答", "论述"}
_AGENT_MARKER = re.compile(r"^TUI_RESULT=(success|blocked)(?:\s+reason=(.*))?\s*$", re.MULTILINE)


class OrganizeBlocked(RuntimeError):
    def __init__(self, status_code: int, reason: str):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason

    @classmethod
    def from_relay_status(cls, status: int) -> "OrganizeBlocked":
        reasons = {
            401: "云端登录已失效，请重新激活后重试。",
            402: "AI 点不足，请兑换新额度后重试。",
            502: "AI 服务暂时没有完成整理，请稍后重试。",
            503: "AI 服务暂未配置，请联系管理员后重试。",
        }
        return cls(status, reasons.get(status, "云端服务暂时不可用，请稍后重试。"))


@dataclass(frozen=True)
class AgentDispatchResult:
    status: str
    reason: str = ""


def agent_dispatch_result(run) -> AgentDispatchResult:
    """Translate the established TUI_RESULT protocol without inventing success."""
    output = run.output or ""
    markers = list(_AGENT_MARKER.finditer(output))
    if run.returncode != 0:
        reason = (run.stderr or output or f"agent exit {run.returncode}").strip()
        return AgentDispatchResult("blocked", reason)
    if not markers:
        return AgentDispatchResult("blocked", "代理返回缺少 TUI_RESULT 标记。")
    if len(markers) != 1:
        return AgentDispatchResult("blocked", "代理返回了多个 TUI_RESULT 标记。")
    marker = markers[0]
    if marker.group(1) == "blocked":
        return AgentDispatchResult("blocked", (marker.group(2) or "代理未执行整理。").strip())
    return AgentDispatchResult("success")


class MemoryOrganizeRecordStore:
    def __init__(self):
        self._records: dict[str, dict] = {}

    def get(self, scope_key: str) -> dict | None:
        value = self._records.get(scope_key)
        return dict(value) if value else None

    def put(self, scope_key: str, record: dict) -> None:
        self._records[scope_key] = dict(record)


class JsonOrganizeRecordStore(MemoryOrganizeRecordStore):
    """Small metadata-only identity store used to make retries no-ops."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            if isinstance(raw, dict):
                self._records = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError, TypeError):
            pass

    def get(self, scope_key: str) -> dict | None:
        with self._lock:
            return super().get(scope_key)

    def put(self, scope_key: str, record: dict) -> None:
        with self._lock:
            super().put(scope_key, record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._records, ensure_ascii=False, indent=2), "utf-8")
            temporary.replace(self.path)


class LocalNotesProvider:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def for_scope(self, *, course_title: str, lecture_titles: list[str]) -> list[dict]:
        wanted = {title.strip() for title in lecture_titles}
        notes: list[dict] = []
        for job in collect_jobs(self.data_dir):
            stage, _ = recording_stage(job)
            if job.course_name != course_title or stage != "notes_ready":
                continue
            if wanted and job.recording.title.strip() not in wanted:
                continue
            path = job.directory / "notes.md"
            try:
                text = path.read_text("utf-8")
            except OSError:
                continue
            if text.strip():
                notes.append({"title": job.recording.title, "source": "课堂录像笔记", "notes": text[:MAX_NOTE_CHARS]})
        return notes


class RealPageAdapter:
    def __init__(self, settings):
        self.settings = settings

    def create_page(self, parent_page_id: str, title: str, *, children: list[dict]) -> dict:
        with get_client(self.settings) as client:
            return client.create_page(parent_page_id, title, children=children)


def _scope_key(course_id: str, lecture_ids: list[str]) -> str:
    raw = json.dumps([course_id, sorted(set(lecture_ids))], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prompt(course_title: str, lecture_titles: list[str], notes: list[dict]) -> str:
    sources = "\n\n".join(
        f"## {item['title']}\n来源：{item['source']}\n{item['notes']}" for item in notes
    )
    prompt = (
        "è¯·æ ¹æ®ä»¥ä¸è¯¾å ç¬è®°æ´çä¸å¥ 5 éç»ä¹ ï¼å¿é¡»åæä¸ä»æä¸ééæ©ãå¤æ­ãå¡«ç©ºãç®ç­ãè®ºè¿°é¢ï¼æ¯é¢å¿é¡»å¸¦æ¥æºã"
        "只返回 JSON：{title, questions:[{type,question,source,answer}]}。\n"
        f"课程：{course_title}\n讲次：{'、'.join(lecture_titles)}\n\n{sources}"
    )
    return prompt[:MAX_PROMPT_CHARS]


def _parse_quiz(content: Any) -> tuple[str, list[dict]]:
    try:
        value = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError) as exc:
        raise OrganizeBlocked(502, "AI 返回的练习格式不完整，请重试。") from exc
    if not isinstance(value, dict) or not isinstance(value.get("questions"), list):
        raise OrganizeBlocked(502, "AI 返回的练习格式不完整，请重试。")
    questions = value["questions"]
    if len(questions) != 5:
        raise OrganizeBlocked(502, "AI 返回的题目数量不符合要求，请重试。")
    normalized: list[dict] = []
    for item in questions:
        if not isinstance(item, dict):
            raise OrganizeBlocked(502, "AI 返回的练习格式不完整，请重试。")
        question = {key: str(item.get(key) or "").strip() for key in ("type", "question", "source", "answer")}
        if question["type"] not in _ALLOWED_TYPES or not all(question.values()):
            raise OrganizeBlocked(502, "AI 返回的练习格式不完整，请重试。")
        normalized.append(question)
    if {item["type"] for item in normalized} != _ALLOWED_TYPES:
        raise OrganizeBlocked(502, "AI 返回的题型不够丰富，请重试。")
    title = str(value.get("title") or "课程练习").strip()
    if not is_exercise_title(title):
        title = f"练习 · {title}"
    return title, normalized


def _blocks(questions: list[dict]) -> list[dict]:
    lines = ["## 练习题"]
    for number, item in enumerate(questions, 1):
        lines.extend([f"### {number}. {item['type']}", item["question"], f"来源：{item['source']}"])
    lines.append("## 教师区（答案）")
    for number, item in enumerate(questions, 1):
        lines.append(f"{number}. 答案：{item['answer']}")
    return markdown_to_blocks("\n\n".join(lines))


class ExerciseOrganizer:
    def __init__(self, *, directory_service, relay, page_adapter, notes_provider,
                 record_store=None, agent_dispatcher: Callable[[dict], Any] | None = None):
        self.directory_service = directory_service
        self.relay = relay
        self.page_adapter = page_adapter
        self.notes_provider = notes_provider
        self.record_store = record_store or MemoryOrganizeRecordStore()
        self.agent_dispatcher = agent_dispatcher
        self._lock = threading.Lock()

    def estimate(self) -> dict:
        return {"label": ESTIMATE_LABEL, "min_points": ESTIMATE_MIN_POINTS,
                "max_points": ESTIMATE_MAX_POINTS}

    def organize(self, course_id: str, lecture_ids: list[str]) -> dict:
        clean_course = (course_id or "").strip()
        clean_lectures = sorted({item.strip() for item in lecture_ids if item and item.strip()})
        if not clean_lectures:
            raise OrganizeBlocked(400, "请至少选择一个已有课堂笔记的讲次。")
        key = _scope_key(clean_course, clean_lectures)
        previous = self.record_store.get(key)
        if previous:
            self.directory_service.register_exercise(_entity_from_record(previous))
            return _result(previous, points_charged=0, points_remaining=_balance(self.relay), reused=True)

        balance = _balance(self.relay)
        if not isinstance(balance, (int, float)):
            raise OrganizeBlocked(503, "暂时无法读取 AI 点余额，请稍后重试。")
        if balance < ESTIMATE_MAX_POINTS:
            raise OrganizeBlocked(402, "AI 点不足，至少需要保留 5 AI 点才能开始整理。")

        data = self.directory_service.ensure_loaded()
        course = next((item for item in data.courses if item.id == clean_course), None)
        if course is None:
            raise OrganizeBlocked(404, "找不到对应的 Notion 课程页，请刷新目录后重试。")
        lectures = [item for item in data.lectures_by_course.get(course.id, []) if item.id in clean_lectures]
        if len(lectures) != len(clean_lectures):
            raise OrganizeBlocked(404, "所选讲次无法映射到该课程，请重新选择。")
        notes = self.notes_provider.for_scope(course_title=course.title,
                                              lecture_titles=[item.title for item in lectures])
        if not notes:
            raise OrganizeBlocked(400, "所选范围没有可用课堂笔记，请先完成讲次整理。")

        target = {"operation": "quiz", "course_id": course.id,
                  "course_name": course.title, "lecture_ids": clean_lectures}
        if self.agent_dispatcher is not None:
            dispatch = agent_dispatch_result(self.agent_dispatcher(target))
            if dispatch.status != "success":
                raise OrganizeBlocked(400, dispatch.reason)

        with self._lock:
            previous = self.record_store.get(key)
            if previous:
                return _result(previous, points_charged=0, points_remaining=_balance(self.relay), reused=True)
            try:
                relay_result = self.relay.quiz(_prompt(course.title, [item.title for item in lectures], notes))
            except OrganizeBlocked:
                raise
            except Exception as exc:
                status = getattr(exc, "status_code", 503)
                raise OrganizeBlocked(status, str(exc) or "云端服务暂时不可用，请稍后重试。") from exc
            title, questions = _parse_quiz(relay_result.get("content"))
            charge = relay_result.get("points_charged")
            if not isinstance(charge, (int, float)):
                raise OrganizeBlocked(502, "云端没有返回本次用量，请重试。")
            page = self.page_adapter.create_page(course.id, title, children=_blocks(questions))
            record = {"id": page["id"], "url": page["url"], "title": title,
                      "course": course.title, "parent": course.id,
                      "scope": " / ".join(item.title for item in lectures),
                      "updated": page.get("last_edited_time", "")}
            self.record_store.put(key, record)
            self.directory_service.register_exercise(_entity_from_record(record))
            return _result(record, points_charged=float(charge),
                           points_remaining=float(balance) - float(charge), reused=False)


def _balance(relay) -> float | None:
    quota = relay.quota()
    value = quota.get("llm_points_remaining") if quota.get("available") else None
    return float(value) if isinstance(value, (int, float)) else None


def _entity_from_record(record: dict) -> ExerciseEntity:
    return ExerciseEntity(id=record["id"], url=record["url"], title=record["title"],
                          course=record["course"], parent=record["parent"],
                          updated=record.get("updated", ""), marker_present=False,
                          answer_present=False)


def _result(record: dict, *, points_charged: float, points_remaining: float | None,
            reused: bool) -> dict:
    return {"status": "completed", "exercise_id": record["id"], "url": record["url"],
            "title": record["title"], "state": "organized",
            "points_charged": points_charged, "points_remaining": points_remaining,
            "reused": reused}


def make_organizer_service(settings, directory_service, relay):
    return ExerciseOrganizer(
        directory_service=directory_service,
        relay=relay,
        page_adapter=RealPageAdapter(settings),
        notes_provider=LocalNotesProvider(settings.data_dir),
        record_store=JsonOrganizeRecordStore(
            Path(settings.data_dir) / "panel" / "organized_exercises.json"
        ),
    )


__all__ = ["ESTIMATE_LABEL", "ESTIMATE_MIN_POINTS", "ESTIMATE_MAX_POINTS",
           "OrganizeBlocked", "AgentDispatchResult", "agent_dispatch_result",
           "MemoryOrganizeRecordStore", "JsonOrganizeRecordStore", "LocalNotesProvider",
           "RealPageAdapter", "ExerciseOrganizer", "make_organizer_service"]
