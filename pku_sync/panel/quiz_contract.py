from __future__ import annotations

import re
import unicodedata

from typing import Any

_ICON_CATEGORIES = {"So", "Sk", "Cf", "Mn"}


def strip_icon(title: str) -> str:
    """Drop leading emoji/symbol decorations (🌐 计算机网络 → 计算机网络)."""
    text = title.strip()
    while text and unicodedata.category(text[0]) in _ICON_CATEGORIES:
        text = text[1:].lstrip()
    return text.strip()


def normalize_title(title: str) -> str:
    """Icon-strip + collapse all whitespace: the exact-text matching key.

    Full-width spaces (U+3000), tabs and repeated spaces collapse to single
    spaces so whitespace variants of the same title match exactly.
    """
    return " ".join(strip_icon(title).split())


EXERCISE_TITLE_MARKERS = ("练习", "小测", "测验", "试题", "习题")


def is_exercise_title(title: str) -> bool:
    normalized = normalize_title(title)
    return any(marker in normalized for marker in EXERCISE_TITLE_MARKERS)


ANSWER_AREA_MARKERS = ("E2E_ANSWER_FIXTURE",)

from .llm_json import extract_single_json_payload

MAX_AGENT_OUTPUT_CHARS = 2000

MAX_PARSE_FAILURE_CAPTURE_BYTES = 8192

QUESTION_TYPES = frozenset({"选择", "判断", "填空", "简答", "论述"})

_E2E_TITLE_RE = re.compile(r"^(?:\[E2E\]\s*)+")

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

