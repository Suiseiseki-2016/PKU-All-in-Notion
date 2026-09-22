"""Metered, idempotent exercise organization for the student panel.

Question material crosses only the local organizer, relay, and Notion adapter.
Panel responses and the directory retain metadata and settlement only.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..notion import get_client, markdown_to_blocks
from ..notion_meta import ExerciseEntity
from ..notion_meta.titles import is_exercise_title
from ..pipeline import collect_jobs, recording_stage
from .llm_json import extract_single_json_payload

ESTIMATE_LABEL = "预计 1–5 AI 点 · 完成后按实际用量结算"
ESTIMATE_MIN_POINTS = 1
ESTIMATE_MAX_POINTS = 5
MAX_NOTE_CHARS = 12000
MAX_PROMPT_CHARS = 100000
MAX_AGENT_OUTPUT_CHARS = 2000
MAX_PARSE_FAILURE_CAPTURE_BYTES = 8192
E2E_TITLE_PREFIX = "[E2E] "
QUESTION_TYPES = frozenset({"选择", "判断", "填空", "简答", "论述"})
_E2E_TITLE_RE = re.compile(r"^(?:\[E2E\]\s*)+")
_AGENT_MARKER = re.compile(r"^TUI_RESULT=(success|blocked)(?:\s+reason=(.*))?\s*$", re.MULTILINE)
# Real synced course.json names carry a trailing semester suffix
# ("计算机网络(26-27学年第1学期)") while Notion course pages use the plain
# title ("计算机网络"), so title resolution strips exactly one trailing
# parenthesized term — half- or full-width — and nothing else.
_TRAILING_TERM_RE = re.compile(r"(?:\([^()]*\)|（[^（）]*）)\Z")


class OrganizeBlocked(RuntimeError):
    def __init__(self, status_code: int, reason: str, *, output: str = "",
                 raw_content: str = ""):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason
        self.output = output[:MAX_AGENT_OUTPUT_CHARS]
        # Bounded raw relay content attached only on an e2e-mode parse
        # failure so a charged op leaves evidence in the blocked payload.
        self.raw_content = bound_relay_content(raw_content)

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
    output: str = ""


def _bounded_agent_output(run, *, preserve_output: bool) -> str:
    """Return output only for an explicitly controlled fake/test dispatcher."""
    if not preserve_output:
        return ""
    output = run.output if isinstance(run.output, str) else ""
    return output[:MAX_AGENT_OUTPUT_CHARS]


def agent_dispatch_result(run, *, preserve_output: bool = False) -> AgentDispatchResult:
    """Translate TUI_RESULT without exposing production stdout or inventing success."""
    raw_output = run.output if isinstance(run.output, str) else ""
    output = _bounded_agent_output(run, preserve_output=preserve_output)
    markers = list(_AGENT_MARKER.finditer(raw_output))
    if run.returncode != 0:
        if not preserve_output:
            legacy_reason = (run.stderr or raw_output or f"agent exit {run.returncode}").strip()
            return AgentDispatchResult("blocked", legacy_reason)
        reasons = {
            69: "\u7ec3\u4e60\u6574\u7406\u4ee3\u7406\u65e0\u6cd5\u8fde\u63a5 MCP\uff0c\u672a\u6267\u884c\u6574\u7406\u3002",
            124: "\u7ec3\u4e60\u6574\u7406\u4ee3\u7406\u7b49\u5f85\u8d85\u65f6\uff0c\u672a\u6267\u884c\u6574\u7406\u3002",
        }
        reason = reasons.get(run.returncode, "\u7ec3\u4e60\u6574\u7406\u4ee3\u7406\u672a\u5b8c\u6210\uff0c\u672a\u6267\u884c\u6574\u7406\u3002")
        return AgentDispatchResult("blocked", reason, output)
    if not markers:
        return AgentDispatchResult("blocked", "\u4ee3\u7406\u8fd4\u56de\u7f3a\u5c11 TUI_RESULT \u6807\u8bb0\u3002", output)
    if len(markers) != 1:
        return AgentDispatchResult("blocked", "\u4ee3\u7406\u8fd4\u56de\u4e86\u591a\u4e2a TUI_RESULT \u6807\u8bb0\u3002", output)
    marker = markers[0]
    if marker.group(1) == "blocked":
        reason = (marker.group(2) or "\u4ee3\u7406\u672a\u6267\u884c\u6574\u7406\u3002").strip()
        return AgentDispatchResult("blocked", reason[:300], output)
    return AgentDispatchResult("success")


def bound_relay_content(content: Any, *,
                        limit_bytes: int = MAX_PARSE_FAILURE_CAPTURE_BYTES) -> str:
    """First UTF-8 bounded slice of the raw relay content, evidence only.

    The slice never ends inside a codepoint, and the only input is the
    metered relay response text — no credentials can appear by construction.
    Unpaired Unicode surrogates, which strict UTF-8 cannot encode, are
    replaced (each surrogate unit becomes a ``?``) so a surrogate-bearing
    response can never raise UnicodeEncodeError out of the diagnostics and
    mask the exact blocked reason.
    """
    if not isinstance(content, str):
        return ""
    raw = content.encode("utf-8", errors="replace")[:limit_bytes]
    while raw:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw[:-1]
    return ""


def persist_parse_failure(directory: Path, content: Any) -> None:
    """Write one read-only evidence file holding the bounded raw quiz content.

    A diagnostics write must never mask the original blocked reason, so the
    capture itself stays inside the best-effort guard: any encoding or
    filesystem failure while bounding or persisting the content is swallowed.
    Each failure gets its own unique file, made read-only after writing.
    """
    try:
        bounded = bound_relay_content(content)
        if not bounded:
            return
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        path = directory / f"quiz-parse-{stamp}-{uuid.uuid4().hex[:8]}.txt"
        path.write_text(bounded, encoding="utf-8")
        os.chmod(path, 0o444)
    except (OSError, UnicodeError):
        pass


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


def normalize_course_title(name: str) -> str:
    """Strip one trailing parenthesized semester suffix; nothing else."""
    return _TRAILING_TERM_RE.sub("", name)


class LocalNotesProvider:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def _local_course_names(self, jobs) -> set[str]:
        """Every local course name that could own a scope's notes.

        course.json names cover synced courses even when they carry no
        recordings (such a course still makes a title ambiguous); the
        recording-job names cover courses without a course.json, whose
        name falls back to the folder name.
        """
        names: set[str] = set()
        for meta in sorted(self.data_dir.glob("*/course.json")):
            try:
                item = json.loads(meta.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            name = str(item.get("name") or "")
            if name:
                names.add(name)
        names.update(job.course_name for job in jobs)
        return names

    def matching_course_names(self, course_title: str, jobs) -> set[str]:
        """Guarded Notion-title → local-course-name resolution.

        Precedent: ``submit.resolve_course_id`` — guarded name resolution
        that refuses ambiguity. Exact local names win; otherwise a
        semester-suffix-stripped match is accepted only when exactly one
        local course normalizes to the Notion title, so zero or multiple
        candidates never yield wrong-course notes.
        """
        local_names = self._local_course_names(jobs)
        exact = {name for name in local_names if name == course_title}
        if exact:
            return exact
        normalized = {name for name in local_names
                      if normalize_course_title(name) == course_title}
        return normalized if len(normalized) == 1 else set()

    def for_scope(self, *, course_title: str, lecture_titles: list[str]) -> list[dict]:
        wanted = {title.strip() for title in lecture_titles}
        jobs = collect_jobs(self.data_dir)
        matched = self.matching_course_names(course_title, jobs)
        notes: list[dict] = []
        for job in jobs:
            stage, _ = recording_stage(job)
            if job.course_name not in matched or stage != "notes_ready":
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


def _scope_key(course_id: str, lecture_ids: list[str], *, e2e_mode: bool = False) -> str:
    scope: list[Any] = [course_id, sorted(set(lecture_ids))]
    if e2e_mode:
        scope.append({"mode": "e2e"})
    raw = json.dumps(scope, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prompt(course_title: str, lecture_titles: list[str], notes: list[dict]) -> str:
    sources = "\n\n".join(
        f"## {item['title']}\n来源：{item['source']}\n{item['notes']}" for item in notes
    )
    prompt = (
        "请根据以下课堂笔记整理一套 5 道练习，必须各有且仅有一道选择、判断、填空、简答、论述题；"
        "JSON 的 type 字段必须严格是以下五个值之一：选择、判断、填空、简答、论述，不得添加“题”后缀；"
        "每题必须带来源。"
        "只返回 JSON：{title, questions:[{type,question,source,answer}]}。\n"
        f"课程：{course_title}\n讲次：{'、'.join(lecture_titles)}\n\n{sources}"
    )
    return prompt[:MAX_PROMPT_CHARS]


def _parse_quiz(content: Any) -> tuple[str, list[dict]]:
    try:
        # The shared bounded recovery tolerates one fence and bounded prose
        # around exactly one JSON object (raw or fenced, CRLF included);
        # every other shape fails here exactly as before.
        value = extract_single_json_payload(content)
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
        if question["type"].endswith("题"):
            question["type"] = question["type"][:-1]
        if question["type"] not in QUESTION_TYPES or not all(question.values()):
            raise OrganizeBlocked(502, "AI 返回的练习格式不完整，请重试。")
        normalized.append(question)
    if {item["type"] for item in normalized} != QUESTION_TYPES:
        raise OrganizeBlocked(502, "AI 返回的题型不够丰富，请重试。")
    title = _E2E_TITLE_RE.sub("", str(value.get("title") or "课程练习").strip())
    if not is_exercise_title(title):
        title = f"练习 · {title}"
    return title, normalized


def _title_for_create(title: str, *, e2e_mode: bool) -> str:
    clean_title = _E2E_TITLE_RE.sub("", title)
    return f"{E2E_TITLE_PREFIX}{clean_title}" if e2e_mode else clean_title


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
                 record_store=None, agent_dispatcher: Callable[[dict], Any] | None = None,
                 preserve_agent_output: bool = False, e2e_enabled: bool = False,
                 parse_failure_dir: Path | None = None):
        self.directory_service = directory_service
        self.relay = relay
        self.page_adapter = page_adapter
        self.notes_provider = notes_provider
        self.record_store = record_store or MemoryOrganizeRecordStore()
        self.agent_dispatcher = agent_dispatcher
        self.preserve_agent_output = bool(preserve_agent_output and agent_dispatcher is not None)
        self.e2e_enabled = e2e_enabled
        self.parse_failure_dir = (
            Path(parse_failure_dir) if parse_failure_dir is not None else None
        )
        self._lock = threading.Lock()

    def estimate(self) -> dict:
        return {"label": ESTIMATE_LABEL, "min_points": ESTIMATE_MIN_POINTS,
                "max_points": ESTIMATE_MAX_POINTS}

    def organize(self, course_id: str, lecture_ids: list[str], *, e2e_mode: bool = False) -> dict:
        if e2e_mode and not self.e2e_enabled:
            raise OrganizeBlocked(403, "E2E 练习整理模式未启用。")
        clean_course = (course_id or "").strip()
        clean_lectures = sorted({item.strip() for item in lecture_ids if item and item.strip()})
        if not clean_lectures:
            raise OrganizeBlocked(400, "请至少选择一个已有课堂笔记的讲次。")
        key = _scope_key(clean_course, clean_lectures, e2e_mode=e2e_mode)
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
                  "course_name": course.title, "lecture_ids": clean_lectures,
                  "e2e_mode": e2e_mode}
        if self.agent_dispatcher is not None:
            dispatch = agent_dispatch_result(
                self.agent_dispatcher(target), preserve_output=self.preserve_agent_output
            )
            if dispatch.status != "success":
                raise OrganizeBlocked(400, dispatch.reason, output=dispatch.output)

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
            try:
                title, questions = _parse_quiz(relay_result.get("content"))
            except OrganizeBlocked as exc:
                # A charged operation lost at parse leaves bounded raw
                # evidence on disk, and echoes it only in e2e_mode.
                if self.parse_failure_dir is not None:
                    persist_parse_failure(self.parse_failure_dir, relay_result.get("content"))
                if e2e_mode:
                    exc.raw_content = bound_relay_content(relay_result.get("content"))
                raise
            charge = relay_result.get("points_charged")
            if not isinstance(charge, (int, float)):
                raise OrganizeBlocked(502, "云端没有返回本次用量，请重试。")
            title = _title_for_create(title, e2e_mode=e2e_mode)
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
        e2e_enabled=bool(getattr(settings, "exercise_e2e_enabled", False)),
        record_store=JsonOrganizeRecordStore(
            Path(settings.data_dir) / "panel" / "organized_exercises.json"
        ),
        parse_failure_dir=Path(settings.data_dir) / "panel" / "organize_failures",
    )


__all__ = ["ESTIMATE_LABEL", "ESTIMATE_MIN_POINTS", "ESTIMATE_MAX_POINTS",
           "MAX_AGENT_OUTPUT_CHARS", "MAX_PARSE_FAILURE_CAPTURE_BYTES",
           "E2E_TITLE_PREFIX", "QUESTION_TYPES",
           "OrganizeBlocked", "AgentDispatchResult", "agent_dispatch_result",
           "bound_relay_content", "persist_parse_failure", "normalize_course_title",
           "MemoryOrganizeRecordStore", "JsonOrganizeRecordStore", "LocalNotesProvider",
           "RealPageAdapter", "ExerciseOrganizer", "make_organizer_service"]
