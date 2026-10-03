"""Validate proposed repairs of fragmentary, independently sourced speech.

The model may locate a clean *verbatim* span in the supplied evidence. This
module never treats a plausible paraphrase or a related slide as proof that a
different word was spoken. Anything requiring that inference needs audio review.
The caller must independently validate the ledger and supplied evidence first.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class FragmentRepairError(ValueError):
    """The proposed repair cannot be safely applied."""


_PROTECTED = re.compile(
    r"作业|题目|习题|附件|提交|截止|考试|测验|考核|考勤|签到|缺勤|"
    r"到场|出席|展示|分组|点名|补交|迟交|必须|务必|老师要求|教师要求|"
    r"下周|下次课|不得|不能|不许|需要|无需|不需要|\d",
    re.I,
)
_FILLER = re.compile(
    r"^(?:好|好的|嗯|呃|啊|对吧|是吧|我们继续|"
    r"(?:我们|大家|同学们)?(?:接着|继续|往下)?"
    r"(?:看一下|看看|来看|看一看)(?:这里|下面|这一页|这张图)?(?:吧|啊)?)$"
)
_UNBOUND = re.compile(r"这三位作者|这一原则|这个思想|这个例子|这个手段|这个变化|这个变换")
_NEGATION = re.compile(r"(?<![A-Za-z])(?:不能|不会|不得|不许|不需要|无需|未必|并非|不是|没有|不可|必须|务必|需要)(?![A-Za-z])")
_ACRONYM = re.compile(r"(?<![A-Za-z])(?:[A-Z]{2,}|rsa|rac|des|aes)(?![A-Za-z])")
_NUMBER = re.compile(r"(?<![A-Za-z\d])\d+(?:\.\d+)?(?:\s*[-~～—]\s*\d+(?:\.\d+)?)?(?![A-Za-z\d])")
_CJK_TERM = re.compile(r"[\u4e00-\u9fff]{2,}(?:算法|密码|定理|原理|标准|协议|理论)")
_NAME = re.compile(r"(?<![A-Za-z])[A-Z][a-z]{2,}(?![A-Za-z])")
_PUNCT = re.compile(r"[\s，,。；;：:、！？!?（）()“”‘’\[\]【】]")


@dataclass(frozen=True)
class FragmentRepairResult:
    kept: list[dict[str, Any]]
    omitted: list[dict[str, Any]]
    needs_audio: list[dict[str, Any]]

    @property
    def ready(self) -> bool:
        return not self.needs_audio


def _compact(value: str) -> str:
    return _PUNCT.sub("", value).casefold()


def _tokens(pattern: re.Pattern[str], value: str) -> set[str]:
    matches = pattern.findall(value)
    if pattern is _CJK_TERM:
        matches = [re.sub(r"^(?:然后|我们|这个|那个|这是|就是)+", "", match)
                   for match in matches]
    return {match.casefold() for match in matches}


def _claim(row: Mapping[str, Any]) -> Mapping[str, Any]:
    claim = row.get("claim", row)
    if not isinstance(claim, Mapping):
        raise FragmentRepairError("review claim must be an object")
    return claim


def _identifier(row: Mapping[str, Any]) -> str:
    value = row.get("id")
    if value is None and type(row.get("index")) is int:
        value = f"R{row['index']}"
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,40}", value):
        raise FragmentRepairError("review row needs a stable id or integer index")
    return value


def _body(row: Mapping[str, Any]) -> str:
    claim = _claim(row)
    value = claim.get("display_text") or claim.get("text")
    if not isinstance(value, str) or not value.strip():
        raise FragmentRepairError("review claim has no text")
    return value.strip()


def _evidence(row: Mapping[str, Any], origin: str) -> str:
    claim = _claim(row)
    if origin == "transcript":
        excerpt = claim.get("transcript_excerpt")
        start, end = claim.get("evidence_start"), claim.get("evidence_end")
        if (not isinstance(excerpt, str) or len(_compact(excerpt)) < 6
                or type(start) not in (int, float) or type(end) not in (int, float)
                or start < 0 or end <= start):
            raise FragmentRepairError("transcript evidence needs exact excerpt and time")
        value = row.get("transcript_text") or excerpt
        if not isinstance(value, str) or _compact(excerpt) not in _compact(value):
            raise FragmentRepairError("transcript excerpt is absent from supplied time window")
    else:
        if not all(claim.get(key) for key in ("source_filename", "source_page", "source_quote")):
            raise FragmentRepairError("slide evidence has no exact locator and quote")
        value = row.get("slide_text") or claim.get("source_quote")
        if _compact(str(claim["source_quote"])) not in _compact(str(value)):
            raise FragmentRepairError("slide quote is absent from the supplied page")
    if not isinstance(value, str) or len(_compact(value)) < 6:
        raise FragmentRepairError("missing exact evidence text")
    return value


def _validate_keep(row: Mapping[str, Any], proposal: Mapping[str, Any]) -> dict[str, Any]:
    original = _body(row)
    origin = proposal.get("evidence_origin")
    if origin not in {"transcript", "slide"}:
        raise FragmentRepairError("keep needs transcript or slide evidence_origin")
    # A slide may document a topic, but cannot resolve a doubtful spoken
    # acronym, a dangling pronoun, or a changed obligation in the recording.
    if origin == "slide" and (row.get("claim", row).get("origin") == "transcript"):
        raise FragmentRepairError("slide cannot replace uncertain spoken evidence")
    evidence = _evidence(row, origin)
    quote = proposal.get("evidence_quote")
    text = proposal.get("text")
    if not isinstance(quote, str) or len(_compact(quote)) < 6 or _compact(quote) not in _compact(evidence):
        raise FragmentRepairError("keep needs a contiguous exact evidence quote")
    if not isinstance(text, str) or len(_compact(text)) < 6 or _compact(text) not in _compact(quote):
        raise FragmentRepairError("repair text must be a contiguous verbatim evidence span")
    if _UNBOUND.search(text):
        raise FragmentRepairError("dangling referent needs audio/context review")
    # Substantive words that often change the meaning of a claim must match
    # the original claim. Exact quotation alone cannot justify changing RAC
    # to RSA or turning '不需要提交' into '需要提交'.
    for pattern, label in ((_NUMBER, "number"), (_ACRONYM, "acronym"),
                           (_NAME, "name"), (_CJK_TERM, "named term"),
                           (_NEGATION, "negation")):
        if _tokens(pattern, text) != _tokens(pattern, original):
            raise FragmentRepairError(f"repair changes {label}")
    if bool(_PROTECTED.search(original)) != bool(_PROTECTED.search(text)):
        raise FragmentRepairError("repair changes a protected coursework or attendance claim")
    if _PROTECTED.search(original) and _compact(text) != _compact(original):
        raise FragmentRepairError("protected claim requires exact original wording")
    return {"id": _identifier(row), "text": text.strip(),
            "evidence_origin": origin, "evidence_quote": quote.strip(),
            "reason": proposal["reason"], "claim": dict(_claim(row))}


def validate_fragment_repairs(
    review_rows: Sequence[Mapping[str, Any]],
    proposals: str | Mapping[str, Any],
    *, selected_facts: Sequence[Mapping[str, Any]] = (),
) -> FragmentRepairResult:
    """Validate one strict decision per review row; reject the batch on error.

    ``proposals`` is ``{"repairs": [{"id", "status", "text", "reason",
    "evidence_origin", "evidence_quote", "duplicate_of"}]}``. Status is
    ``keep``, ``omit`` or ``needs_audio``. Fields not applicable to a status
    must be null. Omission is possible only for obvious navigation filler or
    an *exact* duplicate of a selected fact. The caller must still check
    evidence-group coverage and run the independent claim audit.
    """
    if any(not isinstance(row, Mapping) for row in review_rows):
        raise FragmentRepairError("review rows must be objects")
    expected = {_identifier(row): row for row in review_rows}
    if len(expected) != len(review_rows):
        raise FragmentRepairError("repeated review id")
    if isinstance(proposals, str):
        candidate = proposals.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", candidate, re.I)
        if fenced:
            candidate = fenced.group(1)
        try:
            proposals = json.loads(candidate)
        except json.JSONDecodeError as exc:
            # Only repair stray ASCII quotes inside one-line explanatory
            # reasons. Evidence quotes, decisions, IDs, and text stay exact.
            changed = False
            repaired = []
            pattern = re.compile(r'^(\s*"reason"\s*:\s*")(.*)("\s*,?\s*)$')
            for line in candidate.splitlines(keepends=True):
                content = line.rstrip("\r\n")
                match = pattern.match(content)
                if match:
                    body = re.sub(r'(?<!\\)"', r'\\"', match.group(2))
                    changed |= body != match.group(2)
                    line = match.group(1) + body + match.group(3) + line[len(content):]
                repaired.append(line)
            if not changed:
                raise FragmentRepairError("invalid JSON") from exc
            try:
                proposals = json.loads("".join(repaired))
            except json.JSONDecodeError as repaired_exc:
                raise FragmentRepairError("invalid JSON") from repaired_exc
    if not isinstance(proposals, Mapping) or set(proposals) != {"repairs"} or not isinstance(proposals["repairs"], list):
        raise FragmentRepairError("invalid repair response schema")
    rows = proposals["repairs"]
    ids = [item.get("id") if isinstance(item, Mapping) else None for item in rows]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise FragmentRepairError("missing, repeated, or unknown repair id")
    selected: dict[str, str] = {}
    for index, row in enumerate(selected_facts):
        if not isinstance(row, Mapping):
            raise FragmentRepairError("selected facts must be objects")
        key = str(row.get("id") or f"S{index}")
        if key in selected:
            raise FragmentRepairError("repeated selected fact id")
        selected[key] = _body(row)
    kept: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    needs_audio: list[dict[str, Any]] = []
    keys = {"id", "status", "text", "reason", "evidence_origin", "evidence_quote", "duplicate_of"}
    for item in rows:
        if not isinstance(item, Mapping) or set(item) != keys:
            raise FragmentRepairError("invalid decision fields")
        row = expected[item["id"]]
        status, reason = item["status"], item["reason"]
        if status not in {"keep", "omit", "needs_audio"} or not isinstance(reason, str) or not reason.strip():
            raise FragmentRepairError("decision needs valid status and reason")
        if status == "keep":
            if item["duplicate_of"] is not None:
                raise FragmentRepairError("kept claim cannot be a duplicate omission")
            kept.append(_validate_keep(row, item))
            continue
        if any(item[key] is not None for key in ("text", "evidence_origin", "evidence_quote")):
            raise FragmentRepairError("non-kept decision includes new content")
        original = _body(row)
        if status == "needs_audio":
            if item["duplicate_of"] is not None:
                raise FragmentRepairError("audio review cannot claim an omission")
            needs_audio.append({"id": item["id"], "reason": reason, "claim": dict(_claim(row))})
            continue
        if _PROTECTED.search(original):
            raise FragmentRepairError("cannot omit protected coursework or attendance")
        duplicate = item["duplicate_of"]
        if duplicate is None:
            if not _FILLER.fullmatch(original.strip(" \t，,。；;！!？?、")):
                raise FragmentRepairError("only obvious navigation filler may be omitted")
            omission_reason = "navigation_filler"
        elif (isinstance(duplicate, str) and duplicate in selected
              and _compact(original) == _compact(selected[duplicate])):
            omission_reason = "exact_selected_duplicate"
        else:
            raise FragmentRepairError("duplicate is not an exact selected fact")
        omitted.append({"id": item["id"], "reason": omission_reason,
                        "duplicate_of": duplicate, "claim": dict(_claim(row))})
    return FragmentRepairResult(kept, omitted, needs_audio)


def build_request(
    review_rows: Sequence[Mapping[str, Any]],
    *, selected_facts: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, str]:
    """Return a bounded system prompt and JSON payload for one repair batch.

    Selected facts without IDs receive stable, positional ``S0``, ``S1``...
    IDs within this batch. Supply the same ordered facts to the validator.
    No output is accepted until ``validate_fragment_repairs`` verifies it.
    """
    if len(review_rows) > 30:
        raise FragmentRepairError("repair batch exceeds 30 review rows")
    compact_rows = []
    seen = set()
    for row in review_rows:
        if not isinstance(row, Mapping):
            raise FragmentRepairError("review rows must be objects")
        identifier = _identifier(row)
        if identifier in seen:
            raise FragmentRepairError("repeated review id")
        seen.add(identifier)
        claim = _claim(row)
        compact_rows.append({
            "id": identifier, "original_claim": _body(row),
            "transcript_excerpt": claim.get("transcript_excerpt"),
            "evidence_start": claim.get("evidence_start"),
            "evidence_end": claim.get("evidence_end"),
            "source_filename": claim.get("source_filename"),
            "source_page": claim.get("source_page"),
            "source_quote": claim.get("source_quote"),
        })
    selected = []
    for index, row in enumerate(selected_facts):
        if not isinstance(row, Mapping):
            raise FragmentRepairError("selected facts must be objects")
        selected.append({"id": str(row.get("id") or f"S{index}"),
                         "text": _body(row)})
    system = (
        "你只核对课堂转写片段，不补写知识。逐条返回 JSON 对象，顶层仅 repairs 数组；"
        "每项严格包含 id,status,text,reason,evidence_origin,evidence_quote,duplicate_of。"
        "status 只能是 keep、omit、needs_audio。keep 的 text 只能从逐字转写证据"
        "连续摘取，可删除开头口头填充词；不得改数字、专名、缩写、否定、作业或到场要求。"
        "keep 的 evidence_origin 必须写 transcript，不要写 transcript_excerpt；"
        "evidence_quote 必须是转写证据中的连续逐字原文。"
        "即使课件有 RSA，转写写 RAC 时仍填 needs_audio，不能由课件推断实际发音。"
        "omit 仅用于明显翻页口头语，或与 selected_facts 去标点空格后逐字相同的事实；"
        "意思相近但措辞不同不能用 duplicate_of 省略，必须填 needs_audio。"
        "后者必须填 duplicate_of。其余不确定项均填 needs_audio。"
        "非 keep 的 text/evidence_origin/evidence_quote 为 null；"
        "非重复省略项的 duplicate_of 为 null。reason 中如有英文双引号必须按 JSON 转义。"
        "必须返回每个 id 一次。不要代码围栏。"
    )
    user = json.dumps({"review_rows": compact_rows, "selected_facts": selected},
                      ensure_ascii=False, separators=(",", ":"))
    return system, user
