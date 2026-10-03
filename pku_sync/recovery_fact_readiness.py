"""Fail-closed preparation of validated recovery claims for note composition.

This does not repair ASR or prove that a claim is true. The upstream ledger
validates evidence. Here, clearly irrelevant speech can be omitted, while
speech that needs context or correction is held for review. In particular, a
slide cannot silently replace an uncovered spoken evidence group.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


_OBLIGATION = re.compile(
    r"作业|题目|习题|附件|提交|截止|DDL|考试|测验|考核|考勤|签到|缺勤|"
    r"到场|出席|展示|分组|点名|补交|迟交|老师要求|教师要求|下周|下次课",
    re.I,
)
_FILLER = re.compile(
    r"(?:好|好的|嗯|呃|啊|对吧|是吧|我们继续|大家(?:接着|继续)?(?:看一下|看看|来看)(?:这里|下面|这一页|这张图)?)"
    r"|(?:接下来|下面)(?:我们)?(?:看|来看看)(?:一些|几个)?.{1,24}(?:例子|案例)"
)
_MARKET_CHATTER = re.compile(r"比特币.{0,30}(?:美元|价格)|(?:美元|价格).{0,30}比特币")
_STUDY_CONTEXT = re.compile(r"密码|加密|哈希|区块链|密钥|签名|算法|安全|攻击|挖矿|交易")
_ASR_NOISE = re.compile(
    r"就就|都都|几几|被被|他他们|它它|一一个|一个一个|可以可以|这这个|"
    r"在缠在|铲解解开|解解开|嗯|呃|呐|嘛|啊",
    re.I,
)
_RAC_TERM = re.compile(r"(?<![A-Za-z])rac\s*算法(?![A-Za-z])", re.I)
_DANGLING = re.compile(
    r"(?:就是|也就是|所以|因为|然后|以及|这个|那个|"
    r"一个|一些|开始研究|设计的|还是这个|一个思想)$"
)
_UNBOUND = re.compile(
    r"^(?:这三位作者|这一原则|这个思想|这个例子|所以在这个例子里)|"
    r"(?:这三位作者|这个思想|那这样子|这个手段|这个变化|这个变换)"
)
_CONTEXT_ONLY = re.compile(
    r"^(?:出现了一个|出现了一部|同时呢也出现了|计算机也出现了|屏幕上的文字|墓碑的这个祭司|"
    r"羊皮在缠在木棍上|然后我们看|过去的古典密码时期|"
    r"算法的安全，就设计)")
_ORAL_REQUIREMENT = re.compile(r"(?:老师|教师|课堂|口头).{0,12}(?:要求|通知|作业|考试|考勤|展示)")


@dataclass(frozen=True)
class FactReadiness:
    selected: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    review_required: list[dict[str, Any]]
    uncovered_groups: list[str]
    protected_selected: int

    @property
    def ready(self) -> bool:
        return bool(self.selected) and not self.review_required and not self.uncovered_groups


def _text(row: Mapping[str, Any]) -> str:
    return str(row.get("display_text") or row.get("text") or "").strip()


def assess_recovery_facts(
    validated_claims: Sequence[Mapping[str, Any]],
    *, expected_group_ids: Sequence[str] | None = None,
) -> FactReadiness:
    """Classify ledger rows without inventing or silently dropping study facts.

    ``selected`` keeps the original order and wording. ``excluded`` holds only
    obvious navigation/filler or strictly identified unrelated market chatter.
    All uncertain transcript fragments go to ``review_required`` and block
    ``ready``. Every expected spoken group needs at least one selected spoken
    fact; slide claims never substitute for spoken-group coverage.
    """
    if any(not isinstance(row, Mapping) for row in validated_claims):
        raise TypeError("validated_claims must contain mappings")
    groups = list(dict.fromkeys(
        str(group) for group in (
            expected_group_ids if expected_group_ids is not None else
            [row.get("evidence_group_id") for row in validated_claims
             if row.get("origin") == "transcript"]
        ) if group
    ))
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    protected_selected = 0
    rsa_source = next((row for row in validated_claims
                       if row.get("origin") in {"slide", "both"}
                       and all(row.get(field) for field in
                               ("source_filename", "source_page", "source_quote"))
                       and re.search(r"(?<![A-Za-z])RSA(?![A-Za-z])",
                                     str(row["source_quote"]), re.I)
                       and all(re.search(name, str(row["source_quote"]), re.I)
                               for name in ("Rivest", "Shamir", "Adleman"))
                       and "公钥" in str(row["source_quote"])), None)
    for index, row in enumerate(validated_claims):
        body = _text(row)
        origin = row.get("origin")
        protected = bool(_OBLIGATION.search(body) or _ORAL_REQUIREMENT.search(body))
        reason: str | None = None
        disposition = "selected"
        if not body:
            reason, disposition = "empty_claim", "review"
        elif origin == "transcript":
            plain = body.strip(" \t，,。；;！!？?、")
            if not protected and _FILLER.fullmatch(plain):
                reason, disposition = "non_study_speech_filler", "excluded"
            elif (not protected and _MARKET_CHATTER.search(body)
                  and not _STUDY_CONTEXT.search(body)):
                reason, disposition = "unrelated_market_chatter", "excluded"
            elif _RAC_TERM.search(body):
                reason, disposition = "spoken_technical_term_needs_audio_review", "review"
            elif (_ASR_NOISE.search(body) or _DANGLING.search(plain)
                  or _UNBOUND.search(body) or _CONTEXT_ONLY.search(body)
                  or len(plain) < 12):
                reason, disposition = "spoken_claim_needs_context_or_asr_review", "review"
        elif origin in {"slide", "both"}:
            if not all(row.get(field) for field in
                       ("source_filename", "source_page", "source_quote")):
                reason, disposition = "slide_source_incomplete", "review"
        else:
            reason, disposition = "unknown_origin", "review"
        entry = {"index": index, "reason": reason, "claim": dict(row)}
        if reason == "spoken_technical_term_needs_audio_review" and rsa_source:
            entry["candidate_source"] = {
                "term": "RSA", "source_filename": rsa_source["source_filename"],
                "source_page": rsa_source["source_page"],
                "source_quote": rsa_source["source_quote"],
            }
        if disposition == "review":
            review.append(entry)
        elif disposition == "excluded":
            excluded.append(entry)
        else:
            selected.append(dict(row))
            if protected:
                protected_selected += 1
    covered = {str(row.get("evidence_group_id")) for row in selected
               if row.get("origin") == "transcript" and row.get("evidence_group_id")}
    return FactReadiness(
        selected=selected,
        excluded=excluded,
        review_required=review,
        uncovered_groups=[group for group in groups if group not in covered],
        protected_selected=protected_selected,
    )
