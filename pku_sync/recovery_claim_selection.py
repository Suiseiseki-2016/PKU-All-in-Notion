"""Conservative selection of validated evidence for a readable recovery note.

Validation of provenance happens upstream.  This module only removes speech
that plainly adds no study content; uncertainty always keeps the claim.  In
particular, a slide can cover a spoken aside only when its own validated claim
contains the same complete assertion, not merely a related topic word.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_PROTECTED = re.compile(
    r"作业|题目|习题|附件|提交|截止|DDL|考试|测验|考核|分数|评分|学分|"
    r"考勤|签到|缺勤|到场|出席|展示|分组|点名|补交|迟交|老师要求|教师要求|"
    r"必须|务必|需要完成|下周|下次课|"
    r"\d|第[一二三四五六七八九十百千万两]|"
    r"[一二三四五六七八九十百千万两](?:年|月|日|周|小时|分钟|页|位|元|次|种|条|点|分|倍|%)",
    re.I,
)
_FILLER = re.compile(
    r"^(?:(?:好|好的|嗯|呃|啊|那个|这个|然后)[，,、 ]*)?"
    r"(?:(?:我们|大家|同学们)[，,、 ]*)?"
    r"(?:接着|继续|往下|先)?(?:看一下|看看|来看|看一看|往下看)"
    r"(?:这里|下面|这一页|这张图|这个)?(?:吧|啊|哈)?$|"
    r"^(?:好|好的|嗯|呃|啊|对吧|是吧|就这样|大概就是这样|我们继续)$"
)
_ASIDE_PREFIX = re.compile(
    r"^(?:(?:那|所以|然后)[，,、 ]*)?"
    r"(?:刚才|前面|之前)(?:我们)?(?:已经)?(?:说|讲|提)(?:过|到)?[，,、：: ]*"
)
_INCOMPLETE_END = re.compile(
    r"(?:就是|也就是|所以|因为|然后|以及|用的还是这个|是这个|"
    r"这个|那个|就|把|跟|对|在|用|它|我们|一些|一个|的)$"
)


def _text(claim: Mapping[str, Any]) -> str:
    return str(claim.get("display_text") or claim.get("text") or "").strip()


def _compact(text: str) -> str:
    return re.sub(r"[\s，,。；;：:、！？!?（）()“”‘’]", "", text)


def _duplicate_key(claim: Mapping[str, Any]) -> tuple[Any, ...]:
    """Identify the same claim repeated at the same evidence location.

    Punctuation-only variants from one transcript block are one study fact,
    not two facts. Keep repeats from different blocks, and keep slide and
    transcript evidence separate so corroboration is not discarded.
    """
    block = claim.get("evidence_block_id") or claim.get("evidence_group_id")
    if block is None:
        block = (
            claim.get("evidence_start"),
            claim.get("evidence_end"),
        )
    return (
        claim.get("origin"),
        claim.get("source_filename"),
        claim.get("source_page"),
        block,
        _compact(_text(claim)),
    )


def _slide_covers(assertion: str, slides: Sequence[Mapping[str, Any]]) -> bool:
    """Require a complete six-Han-character assertion on a separate slide.

    Exact text continuity is intentionally stricter than topic similarity.
    The ledger's ``slide`` origin and quote identify independently validated
    slide evidence; transcript-only or mixed-origin claims do not qualify.
    """
    needle = _compact(assertion)
    if len(re.findall(r"[\u4e00-\u9fff]", needle)) < 6:
        return False
    for slide in slides:
        if slide.get("origin") != "slide":
            continue
        if not all(slide.get(key) for key in ("source_filename", "source_page", "source_quote")):
            continue
        slide_text = _compact(_text(slide))
        quote = _compact(str(slide["source_quote"]))
        if needle in slide_text and needle in quote:
            return True
    return False


def select_recovery_claims(
    validated_claims: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (selected claims, omissions with original index and reason).

    This is a presentation filter, not a new evidence validator. It preserves
    order and never mutates ledger records. A caller must still check chapter
    coverage and all coursework/attendance requirements after selection.
    """
    if any(not isinstance(claim, Mapping) for claim in validated_claims):
        raise TypeError("validated_claims must contain mappings")
    slides = [claim for claim in validated_claims if claim.get("origin") == "slide"]
    selected: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    seen_duplicates: set[tuple[Any, ...]] = set()
    for index, claim in enumerate(validated_claims):
        body = _text(claim)
        reason: str | None = None
        duplicate_key = _duplicate_key(claim)
        if duplicate_key in seen_duplicates:
            omissions.append({"index": index, "reason": "duplicate_validated_claim",
                              "claim": dict(claim)})
            continue
        seen_duplicates.add(duplicate_key)
        # Even an apparent aside can contain an assignment, a room requirement,
        # or a numeric fact. Such records are never selected for deletion.
        if claim.get("origin") == "transcript" and not _PROTECTED.search(body):
            plain = body.strip(" \t，,。；;！!？?、")
            if _FILLER.fullmatch(plain):
                reason = "non_study_speech_filler"
            else:
                aside = _ASIDE_PREFIX.match(plain)
                if aside and _slide_covers(plain[aside.end():], slides):
                    reason = "repeated_aside_covered_by_verified_slide"
                elif _INCOMPLETE_END.search(plain) and _slide_covers(plain, slides):
                    reason = "incomplete_fragment_covered_by_verified_slide"
        if reason:
            omissions.append({"index": index, "reason": reason, "claim": dict(claim)})
        else:
            selected.append(dict(claim))
    return selected, omissions
