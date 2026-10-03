"""Compose readable paragraphs from already validated, atomic lecture facts.

The model is an editor, not an evidence authority. ``build_request`` supplies an
exact JSON contract; ``validate_and_render`` checks that every input fact appears
once in a sentence with matching provenance and appends slide citations from the
input records. A successful result still needs the separate semantic claim audit:
local lexical checks cannot prove that a paraphrase preserves every implication.
No network operation is performed in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePath
import re
from typing import Any


_ORIGINS = {"slide", "transcript", "both"}
_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_ACRONYM_RE = re.compile(r"(?<![A-Za-z])[A-Z][A-Z0-9]{1,}(?![A-Za-z])")
_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?%?(?![A-Za-z])")
_LATIN_RE = re.compile(r"(?<![A-Za-z])[A-Za-z][A-Za-z'-]{2,}(?![A-Za-z])")
_CITATION_RE = re.compile(r"(?:参考课件|课件：|课件:|第\s*\d+\s*页|\[\^|\[\d+\])")
_ORAL_ATTRIBUTION_RE = re.compile(r"(?:老师|教师|授课者|课堂上(?:说|提|讲)|讲课时)")
_TIMECODE_RE = re.compile(r"(?:\d{1,2}:\d{2}(?::\d{2})?|\d+\s*[-—～~]\s*\d+\s*分钟)")
_RELATIONAL_CUES = ("因此", "由此", "从而", "导致", "体现", "证明", "奠定", "推动", "意味着")


class CompositionError(ValueError):
    """The model output cannot be published as a grounded note."""


@dataclass(frozen=True)
class ComposedSentence:
    text: str
    fact_ids: tuple[str, ...]
    origin: str
    source_labels: tuple[str, ...]


@dataclass(frozen=True)
class CompositionResult:
    markdown: str
    sentences: tuple[ComposedSentence, ...]
    used_fact_ids: tuple[str, ...]


def _page_label(raw: Any) -> str:
    if not isinstance(raw, (str, int)) or isinstance(raw, bool):
        raise CompositionError("source_page must be a page number or page label")
    value = str(raw).strip()
    match = re.fullmatch(r"(?:第\s*)?(\d+)(?:\s*页)?", value)
    if not match or int(match.group(1)) < 1:
        raise CompositionError(f"unsupported source_page: {value!r}")
    return f"第{int(match.group(1))}页"


def _source_name(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CompositionError("slide fact is missing source_filename")
    name = PurePath(raw.replace("\\", "/")).name.strip()
    if (not name or name in {".", ".."}
            or any(char in name for char in "\r\n[]（）()<>`")):
        raise CompositionError("unsafe source_filename")
    return name


def _normalize_facts(facts: list[dict]) -> list[dict]:
    if not isinstance(facts, list) or not facts:
        raise CompositionError("facts must be a nonempty list")
    normalized = []
    seen = set()
    for position, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise CompositionError(f"fact {position} is not an object")
        identity = fact.get("id")
        origin = fact.get("origin")
        display = fact.get("display_text")
        if not isinstance(identity, str) or not _ID_RE.fullmatch(identity) or identity in seen:
            raise CompositionError(f"invalid or repeated fact ID: {identity!r}")
        if origin not in _ORIGINS:
            raise CompositionError(f"invalid origin for {identity}")
        if not isinstance(display, str) or not display.strip() or "\n" in display:
            raise CompositionError(f"invalid display_text for {identity}")
        seen.add(identity)
        row = {"id": identity, "origin": origin, "display_text": display.strip()}
        if origin in {"slide", "both"}:
            row["source_filename"] = _source_name(fact.get("source_filename"))
            row["source_page"] = _page_label(fact.get("source_page"))
        elif fact.get("source_filename") or fact.get("source_page"):
            raise CompositionError(f"transcript fact {identity} has slide provenance")
        normalized.append(row)
    return normalized


def build_request(facts: list[dict], *, title: str = "") -> tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for a strict JSON editing call.

    Fact records need ``id``, ``origin`` (transcript/slide/both), and
    ``display_text``. Slide/both records also need ``source_filename`` and
    ``source_page``. Pass only independently validated, atomic facts. The model
    must return ``{"paragraphs":[{"sentences":[{"text": ..., "fact_ids":
    [...], "origin": ...}]}]}``; citations are added locally, not by the model.
    """
    rows = _normalize_facts(facts)
    if not isinstance(title, str) or "\n" in title or len(title) > 160:
        raise CompositionError("invalid title")
    system = (
        "你是给缺课学生写自然中文课堂笔记的编辑。只改写提供的已核验事实，不增加事实、"
        "数字、术语、因果、事实之间的关系、例子、评价或老师没有说过的话。"
        "不要推论某个原则体现在后续事件中，也不要用因此、体现、推动之类连接词添加关系。"
        "不要写时间轴、逐分钟记录或列表。按概念和例子的关系写自然段，"
        "不要把每条事实机械地单列成一段；事实较少时写一段，较多时分成两至三段。"
        "每句话引用一至两个紧密相关的事实编号；只有同一种 origin，且课件来源文件"
        "和页码相同的事实才能合并。每个事实编号恰好使用一次，不能遗漏或捏造编号。"
        "合并时也不能增加输入没有明确支持的因果、顺序或评价。单句最多180字。"
        "slide 事实不可写成老师口头所说，transcript 事实不可写成课件内容；both 表示两者都支持。"
        "句子里不要自己添加课件页码或引用，程序会从事实记录绑定来源。"
        "只输出严格 JSON 对象，唯一顶层键是 paragraphs；每段唯一键是 sentences；"
        "每句恰有 text、fact_ids、origin 三个键。"
    )
    user = json.dumps({"title": title.strip(), "facts": rows,
                       "output_example": {"paragraphs": [{"sentences": [
                           {"text": "自然句子。", "fact_ids": [rows[0]["id"]],
                            "origin": rows[0]["origin"]}]}]}}, ensure_ascii=False)
    return system, user


def _number_tokens(text: str) -> set[str]:
    compact = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    tokens = {m.group(0) for m in _NUMBER_RE.finditer(compact)}
    # An English source may write 1971-73 while a natural sentence spells out
    # 1971—1973. The latter is the same range, not an invented year.
    for match in re.finditer(r"(?<!\d)(\d{4})\s*[-—–～~]\s*(\d{2})(?!\d)", compact):
        tokens.discard(match.group(2))
        tokens.add(match.group(1)[:2] + match.group(2))
    return tokens


def _han_bigrams(text: str) -> set[str]:
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    return {chunk[i:i + 2] for chunk in chunks for i in range(len(chunk) - 1)}


def _latin_gloss_terms(text: str) -> set[str]:
    """English terms written as a parenthetical gloss of a Chinese term.

    Security and computing courses are taught bilingually, so 明文（plaintext）
    is how a student writes the term down.  The Chinese head word carries the
    assertion and is checked against the evidence like any other wording; the
    bracketed English only names it.  Treating that gloss as an invented term
    deletes correct sentences, which is the opposite of protecting the note.
    """
    terms: set[str] = set()
    for match in re.finditer(r"[\u4e00-\u9fff]\s*[（(]([^）)]{1,40})[）)]", text):
        terms.update(word.casefold() for word in _LATIN_RE.findall(match.group(1)))
    return terms


def _invented_latin_names(sentence: str, support: str) -> bool:
    """Whether every Latin term in a sentence is accounted for.

    Distinct Latin names are high-signal evidence of fabrication when asserted
    in running prose; common connector words carry no factual claim.
    """
    ignored = {"the", "and", "for", "with", "from", "into", "that", "this"}
    supported = {word.casefold() for word in _LATIN_RE.findall(support)}
    supported |= ignored | _latin_gloss_terms(sentence)
    return all(word.casefold() in supported for word in _LATIN_RE.findall(sentence))


def _check_grounding(sentence: str, facts: list[dict]) -> None:
    support = " ".join(row["display_text"] for row in facts)
    if any(cue in sentence and cue not in support for cue in _RELATIONAL_CUES):
        raise CompositionError("sentence invents a relationship")
    if not _number_tokens(sentence) <= _number_tokens(support):
        raise CompositionError("sentence invents a number")
    if not set(_ACRONYM_RE.findall(sentence)) <= set(_ACRONYM_RE.findall(support)):
        raise CompositionError("sentence invents a technical acronym")
    if not _invented_latin_names(sentence, support):
        raise CompositionError("sentence invents a Latin technical term or name")
    sentence_bigrams = _han_bigrams(sentence)
    support_bigrams = _han_bigrams(support)
    if support_bigrams and sentence_bigrams:
        overlap = len(sentence_bigrams & support_bigrams) / len(sentence_bigrams)
        if overlap < 0.35:
            raise CompositionError("sentence is lexically ungrounded")
        for clause in re.split(r"[，；：。！？]", sentence):
            clause_bigrams = _han_bigrams(clause)
            if len(clause_bigrams) >= 5:
                clause_overlap = len(clause_bigrams & support_bigrams) / len(clause_bigrams)
                if clause_overlap < 0.25:
                    raise CompositionError("sentence contains an ungrounded clause")
    # Each covered fact must leave a recognizable trace in the sentence. This
    # catches an ID list that claims coverage while silently dropping content.
    for row in facts:
        display = row["display_text"]
        distinctive_numbers = _number_tokens(display)
        if distinctive_numbers and not distinctive_numbers <= _number_tokens(sentence):
            raise CompositionError(f"sentence omits number in {row['id']}")
        distinctive_acronyms = set(_ACRONYM_RE.findall(display))
        if distinctive_acronyms and not distinctive_acronyms <= set(_ACRONYM_RE.findall(sentence)):
            raise CompositionError(f"sentence omits acronym in {row['id']}")
        fact_bigrams = _han_bigrams(display)
        if len(fact_bigrams) >= 3 and sentence_bigrams:
            if len(fact_bigrams & sentence_bigrams) / len(fact_bigrams) < 0.22:
                raise CompositionError(f"sentence appears to omit fact {row['id']}")


def validate_and_render(facts: list[dict], response: str | dict) -> CompositionResult:
    """Fail closed on malformed, missing, repeated, or ungrounded sentence rows.

    The returned Markdown has natural paragraphs with source labels attached to
    their exact slide-supported sentences. ``sentences`` retains explicit IDs and
    origin for downstream independent semantic audit and quality review.
    """
    rows = _normalize_facts(facts)
    by_id = {row["id"]: row for row in rows}
    if isinstance(response, str):
        candidate = response.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", candidate)
        if fenced:
            candidate = fenced.group(1)
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise CompositionError("response is not strict JSON") from exc
    else:
        payload = response
    if not isinstance(payload, dict) or set(payload) != {"paragraphs"}:
        raise CompositionError("invalid response schema")
    paragraphs = payload["paragraphs"]
    if not isinstance(paragraphs, list) or not paragraphs:
        raise CompositionError("paragraphs must be a nonempty list")
    if len(rows) >= 6 and len(paragraphs) < 2:
        raise CompositionError("six or more facts need multiple paragraphs")
    rendered_paragraphs = []
    composed = []
    used = []
    for paragraph in paragraphs:
        if not isinstance(paragraph, dict) or set(paragraph) != {"sentences"}:
            raise CompositionError("invalid paragraph schema")
        sentence_rows = paragraph["sentences"]
        if not isinstance(sentence_rows, list) or not sentence_rows:
            raise CompositionError("paragraph has no sentences")
        rendered = []
        for entry in sentence_rows:
            if not isinstance(entry, dict) or set(entry) != {"text", "fact_ids", "origin"}:
                raise CompositionError("invalid sentence schema")
            sentence = entry["text"]
            identities = entry["fact_ids"]
            origin = entry["origin"]
            if (not isinstance(sentence, str) or not sentence.strip()
                    or "\n" in sentence or len(sentence) > 180
                    or not isinstance(identities, list) or not identities
                    or len(identities) > 2
                    or not all(isinstance(identity, str) for identity in identities)
                    or origin not in _ORIGINS):
                raise CompositionError("invalid sentence content")
            sentence = sentence.strip()
            if _CITATION_RE.search(sentence) or _TIMECODE_RE.search(sentence):
                raise CompositionError("model inserted a source label or timestamp")
            if any(char in sentence for char in "#*`[]"):
                raise CompositionError("sentence contains Markdown or unsupported markup")
            if not sentence.endswith(("。", "！", "？")):
                raise CompositionError("sentence needs terminal punctuation")
            if any(mark in sentence[:-1] for mark in "。！？"):
                raise CompositionError("one sentence row contains multiple sentences")
            if len(set(identities)) != len(identities) or any(identity in used for identity in identities):
                raise CompositionError("fact ID is repeated")
            if any(identity not in by_id for identity in identities):
                raise CompositionError("sentence cites unknown fact ID")
            backing = [by_id[identity] for identity in identities]
            if {row["origin"] for row in backing} != {origin}:
                raise CompositionError("sentence origin does not match fact provenance")
            if origin in {"slide", "both"} and len({
                    (row["source_filename"], row["source_page"]) for row in backing}) != 1:
                raise CompositionError("sentence merges different slide pages")
            if origin == "slide" and _ORAL_ATTRIBUTION_RE.search(sentence):
                raise CompositionError("slide-only fact attributed to teacher")
            _check_grounding(sentence, backing)
            labels = tuple(dict.fromkeys(
                f"{row['source_filename']} {row['source_page']}" for row in backing
                if row["origin"] in {"slide", "both"}))
            citation = f"（参考课件：{'；'.join(labels)}）" if labels else ""
            rendered.append(sentence + citation)
            composed.append(ComposedSentence(sentence, tuple(identities), origin, labels))
            used.extend(identities)
        rendered_paragraphs.append("".join(rendered))
    if set(used) != set(by_id) or len(used) != len(by_id):
        missing = sorted(set(by_id) - set(used))
        raise CompositionError(f"omitted facts: {', '.join(missing)}")
    return CompositionResult("\n\n".join(rendered_paragraphs), tuple(composed), tuple(used))


def render_reader_markdown(composed: CompositionResult) -> str:
    """Show compact sentence references while retaining exact source metadata.

    The audit should continue using ``composed.markdown``; this reading view
    shortens the repeated file/page text without removing citation markers.
    """
    labels: dict[str, int] = {}
    body = composed.markdown
    for sentence in composed.sentences:
        if not sentence.source_labels:
            continue
        for label in sentence.source_labels:
            labels.setdefault(label, len(labels) + 1)
        full = f"（参考课件：{'；'.join(sentence.source_labels)}）"
        compact = "".join(f"〔{labels[label]}〕" for label in sentence.source_labels)
        if full not in body:
            raise CompositionError("composed source label is missing from Markdown")
        body = body.replace(full, compact, 1)
    if not labels:
        return body
    legend = "\n".join(f"〔{index}〕{label}" for label, index in labels.items())
    return f"{body}\n\n资料出处\n\n{legend}"
