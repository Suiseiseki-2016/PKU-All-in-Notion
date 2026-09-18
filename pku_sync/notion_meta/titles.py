"""Title normalization and the verified hub/course/lecture title patterns.

Pages carry only a title property (verified — nothing else to query), so the
directory recognizes structure by parent chain plus these pinned title rules.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# -- normalization -----------------------------------------------------------

# Leading icon decorations: emoji/symbols (So), modifier keys (Sk), invisible
# format chars like ZWJ/variation selectors (Cf), combining marks (Mn).
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


# -- hub family + semester ------------------------------------------------------


def is_hub_family(title: str) -> bool:
    """'Class Notes …' family; tolerates the verified 'Clash Notes' typo."""
    normalized = normalize_title(title).lower()
    return normalized.startswith(("class notes", "clash notes"))


def current_semester_label(now=None) -> str:
    """Calendar-half semester label: 2026-09 → '2026 下半学期'.

    The real hub 'Class Notes 2026 下半学期' is the fall (= second calendar
    half) semester, matching 'Clash Notes 2026上半学期' for spring.
    """
    import datetime

    moment = now or datetime.datetime.now()
    half = "上半学期" if moment.month <= 6 else "下半学期"
    return f"{moment.year} {half}"


def matches_semester(title: str, semester: str) -> bool:
    """Space-insensitive containment ('2026下半学期' in 'Class Notes 2026 下半学期')."""
    return _nospace(semester) in _nospace(normalize_title(title))


def _nospace(text: str) -> str:
    return re.sub(r"\s+", "", text)


# -- special hub children -------------------------------------------------------


def is_learning_center(title: str) -> bool:
    """The 学习中心 sibling: title pattern '…学期学习中心'."""
    return normalize_title(title).endswith("学期学习中心")


def is_recording_notes_hub(title: str) -> bool:
    return "课堂录像笔记" in normalize_title(title)


def is_noise_child(title: str) -> bool:
    """Dated-suffix hub children are log/experience pages, not courses.

    Verified real noise: 'Blackboard 作业提交自动化经验（2026-09-12）'.
    Course pages carry persistent names; a trailing （YYYY-MM-DD） marks a
    dated diary/report page. Excluded titles stay visible in diagnostics.
    """
    return bool(_DATED_SUFFIX_RE.search(normalize_title(title)))


_DATED_SUFFIX_RE = re.compile(r"[（(]\s*\d{4}-\d{1,2}-\d{1,2}\s*[）)]\s*$")


# -- exercise pages ---------------------------------------------------------------

# Exercise-family title vocabulary: the product/prototype copy says 练习, the
# quiz runbook says 小测; 测验/试题/习题 are the same family. Lecture
# recognition runs FIRST, so a lecture-titled child is never typed as an
# exercise even when its title mentions the markers.
EXERCISE_TITLE_MARKERS = ("练习", "小测", "测验", "试题", "习题")


def is_exercise_title(title: str) -> bool:
    normalized = normalize_title(title)
    return any(marker in normalized for marker in EXERCISE_TITLE_MARKERS)


# -- lecture titles --------------------------------------------------------------


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _chinese_number(raw: str) -> int | None:
    """'1'/'一'/'二十四' → int; None when the numeral cannot be parsed."""
    if raw.isdigit():
        return int(raw)
    total = section = 0
    for ch in raw:
        if ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        elif ch == "十":
            section = (section or 1) * 10
            total += section
            section = 0
        elif ch == "百":
            section = (section or 1) * 100
            total += section
            section = 0
        else:
            return None
    return total + section


@dataclass(frozen=True)
class LectureTitle:
    number: int
    date: str = ""  # YYYY-MM-DD from the （…）suffix, when present
    period: str = ""  # e.g. 第1-2节


_LECTURE_RE = re.compile(
    r"^[《「『]?\s*第\s*(?P<num>[0-9]+|[零一二三四五六七八九十百]+)\s*讲"
)
_PAREN_RE = re.compile(
    r"[（(]\s*(?P<date>\d{4}-\d{2}-\d{2})?\s*"
    r"(?P<period>第\s*\d+(?:\s*[-–—]\s*\d+)?\s*节)?\s*[）)]\s*[》」』]?\s*$"
)


def parse_lecture_number(raw: str) -> int | None:
    """Public numeral parse for lecture-number tokens ('一'/'1'/'二十四')."""
    return _chinese_number(raw)


def parse_lecture_title(title: str) -> LectureTitle | None:
    """Parse the three verified lecture-title variants (icons tolerated).

    第一讲 · 计算机网络                          (no 《》, no date suffix)
    第二讲 · 计算机网络（2026-09-09 第1-2节）     (no 《》)
    《第三讲 · 计算机网络（2026-09-14 第5-6节）》 (full format)
    Non-lecture children (《课程总结》 …) return None.
    """
    normalized = normalize_title(title)
    match = _LECTURE_RE.match(normalized)
    if not match:
        return None
    number = _chinese_number(match.group("num"))
    if number is None:
        return None
    date, period = "", ""
    tail = _PAREN_RE.search(normalized[match.end():])
    if tail:
        date = (tail.group("date") or "").strip()
        period = re.sub(r"\s+", "", tail.group("period") or "")
    return LectureTitle(number=number, date=date, period=period)
