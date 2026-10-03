"""Bounded, evidence-linked audit of generated lecture prose.

This is a diagnostic gate: a model's positive verdict is evidence to inspect,
not proof that a lecture note is correct. Missing or malformed verdicts fail.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable


POLICY = "lecture-claim-audit-v13"
MAX_CLAIMS_PER_CALL = 4
MAX_CLAIMS_PER_CHAPTER = 72
MAX_EVIDENCE_PER_CLAIM = 3


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _substantial_quote(value: str, source_text: str = "") -> bool:
    """Accept a short exact numeric slide label without padding it with unrelated text."""
    compact = _compact(value)
    return (len(compact) >= 6 or
            (len(compact) >= 5 and re.search(r"\d", compact)
             and len(re.findall(r"[\u4e00-\u9fff]", compact)) >= 3) or
            (len(compact) == 4 and len(re.findall(r"[\u4e00-\u9fff]", compact)) == 4
             and any(line.strip() == value.strip() for line in source_text.splitlines())))


def _parse_model_json(raw: str) -> dict:
    """Accept one JSON object, optionally wrapped in a lone JSON code fence."""
    candidate = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", candidate, re.I)
    if fenced:
        candidate = fenced.group(1)
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("response_is_not_object")
    return parsed


def _repair_reason_quotes(raw: str) -> dict | None:
    """Repair stray ASCII quotes only inside a single-line ``reason`` value.

    Model prose occasionally contains a quoted phrase with one unescaped
    ASCII quote. Never alter evidence quotes, IDs, or structural fields. The
    ordinary schema and verbatim-evidence checks still run after this repair.
    """
    pattern = re.compile(r'("reason"\s*:\s*")(.*?)("(?=\s*[,}]))')
    changed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        body = match.group(2)
        if "\n" in body or "\r" in body:
            return match.group(0)
        escaped = re.sub(r'(?<!\\)"', r'\\"', body)
        changed |= escaped != body
        return match.group(1) + escaped + match.group(3)

    repaired = pattern.sub(replace, raw)
    if not changed:
        return None
    try:
        return _parse_model_json(repaired)
    except (ValueError, TypeError):
        return None


def _escape_evidence_quote_lines(raw: str) -> str | None:
    """Only escape internal quotes on a single-line evidence quote field."""
    changed = False
    repaired: list[str] = []
    pattern = re.compile(r'^(.*"quote"\s*:\s*")(.*)("\s*[,}]\s*)$')
    for line in raw.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        match = pattern.match(content)
        if match:
            body = re.sub(r'(?<!\\)"', r'\\"', match.group(2))
            changed |= body != match.group(2)
            line = match.group(1) + body + match.group(3) + line[len(content):]
        repaired.append(line)
    if not changed:
        return None
    return "".join(repaired)


def _repair_evidence_quotes(raw: str) -> dict | None:
    """Repair quote JSON, subject to the usual exact source check afterward."""
    repaired = _escape_evidence_quote_lines(raw)
    if repaired is None:
        return None
    try:
        return _parse_model_json(repaired)
    except (ValueError, TypeError):
        return None


def _parse_or_repair_model_json(raw: str) -> dict | None:
    try:
        return _parse_model_json(raw)
    except (ValueError, TypeError):
        return (_repair_reason_quotes(raw) or _repair_evidence_quotes(raw)
                or _repair_reason_quotes_after_evidence(raw))


def _repair_reason_quotes_after_evidence(raw: str) -> dict | None:
    """Handle responses containing both narrowly repairable quote defects."""
    repaired = _escape_evidence_quote_lines(raw)
    return _repair_reason_quotes(repaired) if repaired is not None else None


def note_snapshot_signature(note: str, transcript: dict, source_context: list[dict] | None,
                            model: str) -> str:
    """Tie a completed audit manifest to the exact note and input evidence."""
    payload = {"policy": POLICY, "note": note, "transcript": transcript,
               "sources": source_context, "model": model}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _cited_page_ids(sentence: str, pages: list[dict]) -> tuple[set[str], list[str]]:
    """Bind a displayed page citation to that page's audit evidence ID."""
    page_ids = {(row["title"], _compact(row["locator"])): row["id"] for row in pages}
    required: set[str] = set()
    missing: list[str] = []
    for citation in re.findall(r"（课件：([^）]+)）", sentence):
        citation = citation.split("；原文：", 1)[0]
        match = re.match(r"([^，；]+)，\s*第\s*(\d+)\s*页", citation)
        if not match:
            missing.append(citation)
            continue
        title = match.group(1).strip()
        for page in re.findall(r"第\s*(\d+)\s*页", citation):
            locator = f"第{int(page)}页"
            evidence_id = page_ids.get((title, locator))
            if evidence_id is None:
                missing.append(f"{title} {locator}")
            else:
                required.add(evidence_id)
    return required, missing


def chapter_assertions(chapter: str) -> list[dict[str, str]]:
    """Enumerate every prose sentence; headings supply context, not claims."""
    result: list[dict[str, str]] = []
    heading = ""
    for line in chapter.splitlines():
        line = line.strip()
        if line.startswith(">"):
            line = line.lstrip("> ").strip()
        if not line:
            continue
        if re.match(r"^#{1,6}\s", line) or (line.startswith("**") and line.endswith("**")):
            heading = re.sub(r"^[#*\s]+|[*\s]+$", "", line)
            continue
        line = re.sub(r"^(?:[-*+]\s+|\d+[.)、]\s+)", "", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        if not line:
            continue
        if re.fullmatch(r"（(?:参考课件|课件)：[^）]+）", line):
            # A standalone source locator is metadata for preceding prose,
            # not a sentence that the source itself must quote verbatim.
            continue
        # Citations commonly follow the sentence-ending full stop. Keep the
        # cited page bound to that assertion instead of splitting it off as
        # standalone metadata, which would let a wrong page escape review.
        line = re.sub(r"([。！？!?])\s*(（(?:参考课件|课件)：[^）]+）)",
                      r"\2\1", line)
        # Split at sentence terminators only. Semicolons and colons often
        # join a condition to its conclusion and must stay in the same claim.
        for sentence in re.split(r"(?<=[。！？!?])\s*", line):
            sentence = sentence.strip()
            if re.fullmatch(r"（(?:参考课件|课件)：[^）]+）", sentence):
                continue
            if sentence and re.search(r"[\w\u4e00-\u9fff]", sentence):
                result.append({"id": f"C{len(result) + 1:03d}",
                               "context": heading, "text": sentence})
    return result


def _validate_batch(rows: object, batch: list[dict], evidence: dict[str, str],
                    pages: list[dict]) -> tuple[str, list[dict], list[dict]]:
    """Validate a whole batch before accepting any of its verdicts."""
    if (not isinstance(rows, list) or len(rows) != len(batch)
            or [row.get("id") for row in rows if isinstance(row, dict)]
            != [claim["id"] for claim in batch]):
        return "missing_or_misordered_verdict", [], []
    issues: list[dict] = []
    relabeled: list[dict] = []
    for claim, row in zip(batch, rows):
        if (not isinstance(row, dict)
                or set(row) != {"id", "status", "evidence", "reason"}
                or not isinstance(row["status"], str)
                or row["status"] not in {"supported", "unsupported", "uncertain"}
                or not isinstance(row["reason"], str)
                or not isinstance(row["evidence"], list)
                or len(row["evidence"]) > MAX_EVIDENCE_PER_CLAIM):
            return "invalid_verdict_schema", [], []
        if row["status"] != "supported":
            continue
        if not row["evidence"]:
            issues.append({"id": row["id"], "reason": "supported_without_evidence"})
        for citation in row["evidence"]:
            if (not isinstance(citation, dict)
                    or set(citation) != {"id", "quote"}
                    or not isinstance(citation["id"], str)
                    or not isinstance(citation["quote"], str)
                    or not _substantial_quote(citation["quote"],
                                              evidence.get(citation["id"], ""))):
                issues.append({"id": row["id"], "reason": "invented_or_missing_quote"})
                continue
            quote = _compact(citation["quote"])
            original_id = citation["id"]
            source_text = _compact(evidence.get(original_id, ""))
            if quote not in source_text:
                # PDF extraction often uses curved typography for a title that
                # the model encloses in ASCII quotes. Correct punctuation only
                # when the entire corrected quote is literally in this source.
                curved = re.sub(r'"([^"\n]+)"', r'“\1”', citation["quote"])
                if curved != citation["quote"] and _compact(curved) in source_text:
                    citation["quote"] = curved
                    quote = _compact(curved)
            if quote not in _compact(evidence.get(original_id, "")):
                matches = [identity for identity, text in evidence.items()
                           if identity[:1] == original_id[:1]
                           and quote in _compact(text)]
                if len(matches) == 1:
                    citation["id"] = matches[0]
                    relabeled.append({"claim_id": row["id"], "from": original_id,
                                      "to": matches[0]})
                else:
                    issues.append({"id": row["id"], "reason": "invented_or_missing_quote"})
        required_pages, missing_pages = _cited_page_ids(claim["text"], pages)
        for label in missing_pages:
            issues.append({"id": row["id"],
                           "reason": f"cited_page_not_supplied:{label}"})
        cited_ids = {citation.get("id") for citation in row["evidence"]
                     if isinstance(citation, dict)
                     and isinstance(citation.get("id"), str)}
        if not required_pages <= cited_ids:
            issues.append({"id": row["id"], "reason": "cited_page_lacks_support_quote"})
        page_by_id = {page["id"]: page for page in pages}
        for page_id in required_pages:
            page_text = str(page_by_id[page_id]["text"])
            for term in re.findall(r"(?<![A-Za-z])(?:RSA|DES|AES|SM4)(?![A-Za-z])|置换密码|换位密码",
                                   claim["text"], flags=re.I):
                if term.casefold() not in page_text.casefold():
                    issues.append({"id": row["id"],
                                   "reason": f"cited_page_missing_named_term:{term}"})
    return "", issues, relabeled


def _slide_fact_review_candidates(batch: list[dict], rows: list[dict],
                                  pages: list[dict]) -> list[dict]:
    """Select only rejected, explicitly cited slide facts for one recheck.

    Literal examples must occur on the cited page. This is a trigger for an
    independent model verdict, never a local decision that the claim is true.
    Claims about what a teacher said or assigned cannot use this route.
    """
    by_id = {page["id"]: page for page in pages}
    candidates = []
    for claim, row in zip(batch, rows):
        if row["status"] == "supported":
            continue
        sentence = claim["text"]
        if re.search(r"老师|教师|讲者|主讲人|讲师|教授|课上|课堂上|口头|授课时|讲授时|这节课里", sentence):
            continue
        page_ids, missing = _cited_page_ids(sentence, pages)
        if missing or len(page_ids) != 1:
            continue
        page = by_id[next(iter(page_ids))]
        literals = re.findall(r'["“]([^"”]{3,})["”]', sentence)
        if not literals or not all(_compact(value) in _compact(page["text"])
                                   for value in literals):
            continue
        candidates.append({"id": claim["id"], "cited_page_id": page["id"],
                           "literal_examples_on_page": literals,
                           "previous_status": row["status"],
                           "previous_reason": row["reason"]})
    return candidates


def _verified_caesar_slide_example(claim: dict, pages: list[dict]) -> dict | None:
    """Verify one exact slide example by its plaintext, ciphertext and shift.

    This does not infer a teacher's oral statement. It applies only when the
    note explicitly cites the matched slide and the complete substitution is
    independently reproducible from the quoted lowercase Latin plaintext.
    """
    sentence = claim["text"]
    if ("经此变换后成为" not in sentence
            or re.search(r"老师|教师|口头|讲授", sentence)):
        return None
    required, missing = _cited_page_ids(sentence, pages)
    if missing or len(required) != 1:
        return None
    page = next(row for row in pages if row["id"] in required)
    if (page["title"] != "ch02-古典密码.pdf"
            or _compact(page["locator"]) != "第7页"
            or "凯撒密表" not in page["text"]):
        return None
    literals = re.findall(r'["“]([^"”]+)["”]', sentence)
    if len(literals) != 2:
        return None
    plaintext, ciphertext = literals
    if (not re.fullmatch(r"[a-z]+(?: [a-z]+)*", plaintext)
            or not re.fullmatch(r"[A-Z]+(?: [A-Z]+)*", ciphertext)
            or plaintext not in page["text"] or ciphertext not in page["text"]):
        return None
    computed = "".join(
        chr((ord(char) - ord("a") + 3) % 26 + ord("A")) if char != " " else " "
        for char in plaintext)
    if computed != ciphertext:
        return None
    return {"id": claim["id"], "status": "supported",
            "evidence": [{"id": page["id"], "quote": plaintext},
                         {"id": page["id"], "quote": ciphertext}],
            "reason": "课件第 7 页逐字列出明密文，逐字符后移 3 位计算与密文完全一致"}


def _valid_quote_subset(claim: dict, row: dict, evidence: dict[str, str],
                        pages: list[dict]) -> list[dict] | None:
    """Find one extraneous bad quote without weakening a required page citation.

    This only selects a candidate for a new model verdict. It never changes
    the original verdict locally or treats the remaining quotes as sufficient.
    """
    if row.get("status") != "supported" or not isinstance(row.get("evidence"), list):
        return None
    required, missing = _cited_page_ids(claim["text"], pages)
    if missing:
        return None
    valid, invalid = [], 0
    for citation in row["evidence"]:
        if (not isinstance(citation, dict) or set(citation) != {"id", "quote"}
                or not isinstance(citation["id"], str)
                or not isinstance(citation["quote"], str)
                or not _substantial_quote(citation["quote"],
                                          evidence.get(citation["id"], ""))):
            invalid += 1
        elif _compact(citation["quote"]) in _compact(evidence.get(citation["id"], "")):
            valid.append(citation)
        else:
            invalid += 1
    if invalid != 1 or not valid or not required <= {item["id"] for item in valid}:
        return None
    return valid


def _review_with_snippet_ids(
    claim: dict, evidence_blocks: list[dict], source_pages: list[dict],
    ask: Callable[[str, str], str], diagnostic: list[str] | None = None,
) -> dict | None:
    """Ask for one semantic verdict using only preverified contiguous snippets.

    The model chooses IDs, never writes a quote. Unknown, duplicated, or
    malformed IDs fail; the caller still applies the ordinary claim validator.
    """
    from .evidence_snippets import (
        build_evidence_snippet_catalog, resolve_evidence_choice_ids,
    )

    ordered_pages = _prioritize_cited_source_pages([claim["text"]], source_pages)[:8]
    catalog = build_evidence_snippet_catalog(
        evidence_blocks, ordered_pages, query=claim["text"])
    if not catalog["snippets"] and not catalog.get("support_bundles"):
        return None
    page_labels = [
        {"id": f"S{index + 1}", "title": str(page.get("title") or ""),
         "locator": str(page.get("locator") or "")}
        for index, page in enumerate(ordered_pages) if page.get("text")
    ]
    required_pages, missing_pages = _cited_page_ids(claim["text"], page_labels)
    available_source_ids = {
        row["source_id"]
        for row in [*catalog["snippets"], *catalog.get("support_bundles", [])]
    }
    if missing_pages or not required_pages <= available_source_ids:
        return None
    system = (
        "你是独立的课堂事实核验员。只凭给出的逐字原文片段，判断 claim 整句的事实、关系、"
        "数字、否定和归属是否获支持。一个片段或一页存在不等于整句成立。"
        "只能选择目录中的 E 片段或 G 证据包 ID，最多三个；不要自己抄写或拼接引文。"
        "claim 若明确引用课件页，supported 必须从每个所引页各选至少一个能支持论断的片段；"
        "不能只选别页或转写证据来支撑该引用。"
        "目录若截断，未列入的来源不能据此认定不存在；证据不足判 uncertain。"
        "不能把课件事实说成老师口述。严格返回单个 JSON："
        "{\"schema_version\":1,\"verdicts\":[{\"id\":\"C001\","
        "\"status\":\"supported\",\"evidence_ids\":[\"E...或G...\"],"
        "\"reason\":\"简短理由\"}]}。不附其他文字。"
    )
    # Short model-facing IDs are easier to copy from a bounded list. Resolve
    # them through the immutable catalog before any quote is accepted.
    presented_snippets = [
        {**row, "id": f"E{index + 1}"}
        for index, row in enumerate(catalog["snippets"])
    ]
    presented_bundles = [
        {**row, "id": f"G{index + 1}"}
        for index, row in enumerate(catalog.get("support_bundles", []))
    ]
    choices = {row["id"] for row in [*presented_snippets, *presented_bundles]}
    payload = {"claims": [claim],
               "snippets": presented_snippets,
               "support_bundles": presented_bundles,
               "required_page_ids": sorted(required_pages),
               "catalog_truncated": catalog["truncated"]}
    try:
        raw = ask(system, json.dumps(payload, ensure_ascii=False))
        if diagnostic is not None:
            diagnostic.append(str(raw)[:12000])
        try:
            answer = _parse_model_json(raw)
        except (ValueError, TypeError):
            answer = _repair_reason_quotes(raw)
            if answer is None:
                raise
        if answer.get("schema_version") != 1:
            return None
        verdicts = answer.get("verdicts")
        if not isinstance(verdicts, list) or len(verdicts) != 1:
            return None
        row = verdicts[0]
        if (not isinstance(row, dict)
                or set(row) != {"id", "status", "evidence_ids", "reason"}
                or row["id"] != claim["id"]
                or row["status"] not in {"supported", "unsupported", "uncertain"}
                or not isinstance(row["reason"], str)
                or not isinstance(row["evidence_ids"], list)
                or len(row["evidence_ids"]) > MAX_EVIDENCE_PER_CLAIM
                or any(not isinstance(identity, str) or identity not in choices
                       for identity in row["evidence_ids"])):
            return None
        if (row["status"] == "supported") != bool(row["evidence_ids"]):
            return None
        presented_catalog = {
            "snippets": presented_snippets,
            "support_bundles": presented_bundles,
        }
        citations = resolve_evidence_choice_ids(
            presented_catalog, row["evidence_ids"])
        if diagnostic is not None:
            diagnostic.append(json.dumps({"selected_ids": row["evidence_ids"],
                                          "citations": citations}, ensure_ascii=False))
    except (ValueError, TypeError, KeyError) as exc:
        if diagnostic is not None:
            diagnostic.append(json.dumps({"snippet_error": str(exc),
                                          "catalog_size": len(catalog["snippets"]),
                                          "model_response": str(raw)[:4000] if 'raw' in locals() else ""},
                                         ensure_ascii=False))
        return None
    return {"id": row["id"], "status": row["status"],
            "evidence": citations, "reason": row["reason"]}


def _prioritize_cited_source_pages(
    claims: list[str], source_pages: list[dict],
) -> list[dict]:
    """Put explicitly cited pages first before stable S IDs are assigned."""
    cited_keys = set()
    for claim in claims:
        for citation in re.findall(r"（(?:参考课件|课件)：([^）]+)）", claim):
            match = re.match(r"([^，；]+)，\s*第\s*(\d+)\s*页", citation)
            if match:
                cited_keys.add((match.group(1).strip(), f"第{int(match.group(2))}页"))
    cited = [
        row for row in source_pages
        if (str(row.get("title") or ""),
            _compact(str(row.get("locator") or ""))) in {
                (title, _compact(locator)) for title, locator in cited_keys
            }
    ]
    return [*cited, *(row for row in source_pages if row not in cited)]


def audit_chapter(
    chapter: str,
    evidence_blocks: list[dict],
    source_pages: list[dict],
    ask: Callable[[str, str], str],
    *,
    cache_path: Path | None = None,
    model: str = "",
) -> dict:
    """Audit all chapter sentences in bounded batches, preserving diagnostics.

    ``ask(system, user)`` is the caller's existing configured AI service.
    Its answer must be strict JSON and every positive answer must include a
    short, locally verifiable quote from a supplied transcript block or page.
    """
    assertions = chapter_assertions(chapter)
    if not assertions:
        return {"status": "unverified", "reason": "no_factual_sentences",
                "assertions": [], "verdicts": []}
    if len(assertions) > MAX_CLAIMS_PER_CHAPTER:
        return {"status": "unverified", "reason": "chapter_claim_limit",
                "assertions": assertions, "verdicts": []}
    blocks = [
        {"id": str(row.get("block_id") or ""), "text": str(row.get("text") or "")[:220]}
        for row in evidence_blocks if row.get("block_id") and row.get("text")
    ]
    source_pages = _prioritize_cited_source_pages(
        [claim["text"] for claim in assertions], source_pages,
    )
    pages = [
        {"id": f"S{i + 1}", "title": str(row.get("title") or ""),
         "locator": str(row.get("locator") or ""),
         "text": str(row.get("text") or "")[:2500]}
        for i, row in enumerate(source_pages[:8]) if row.get("text")
    ]
    if sum(len(row["text"]) for row in pages) > 12000:
        remaining = 12000
        for row in pages:
            row["text"] = row["text"][:remaining]
            remaining -= len(row["text"])
    evidence = {row["id"]: row["text"] for row in [*blocks, *pages]}
    signature = hashlib.sha256(json.dumps(
        {"policy": POLICY, "model": model, "assertions": assertions,
         "blocks": blocks, "pages": pages}, ensure_ascii=False,
        sort_keys=True).encode("utf-8")).hexdigest()
    cached: dict = {}
    if cache_path and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text("utf-8"))
            if (cached.get("signature") == signature
                    and cached.get("status") in {"pass", "fail"}
                    and len(cached.get("verdicts", [])) == len(assertions)
                    and not cached.get("reason")
                    and not cached.get("validation_issues")):
                return cached
        except (ValueError, OSError):
            pass
    if cached.get("signature") != signature:
        cached = {}

    system = (
        "你是独立的课堂笔记事实核验员。逐条核验 claims 中整句的所有事实、关系、数字、"
        "正负号、单位、否定、时间和归属。只能依据给出的自动转写 blocks 与匹配课件 pages。"
        "课件事实不可冒充老师口述。课件页存在不等于支持整句；引句必须真的包含支持关系。"
        "自动转写可能把相邻术语混在一起；若 block 对同一概念同时说出两个不同名称，"
        "或与课件的分类冲突，不能仅凭字面相似就判 supported。"
        "对于 X 相当于 Y、X 导致 Y 等关系句，证据必须明确支持关系及主语；"
        "含混时判 uncertain。"
        "若有任一部分不获明确支持、证据相反、内部矛盾、转写含混或只是合理推断，"
        "status 必须是 unsupported 或 uncertain。不要借用常识补证。"
        "每条 claim 恰好一个 verdict，按原编号返回。supported 必须提供最多三个短证据引句，"
        "每个 quote 必须从对应 block/page 复制连续的至少六个原文字符；"
        "含数字且至少三个汉字的完整标签允许五字，例如‘加密密钥3’。"
        "不能摘写、改字、改标点或杜撰；拿不出逐字引句就填 uncertain。"
        "严格 JSON：{\"schema_version\":1,\"verdicts\":[{\"id\":\"C001\","
        "\"status\":\"supported\",\"evidence\":[{\"id\":\"B0001\",\"quote\":\"原文\"}],"
        "\"reason\":\"简短理由\"}]}。不要 Markdown。"
    )
    verdicts: list[dict] = []
    accepted_batches: dict[str, dict] = cached.get("accepted_batches", {})
    if not isinstance(accepted_batches, dict):
        accepted_batches = {}
    issue = ""
    validation_issues: list[dict[str, str]] = []
    relabeled_evidence: list[dict[str, str]] = []
    invalid_response_excerpt = ""
    repairable_quote_reasons = {"invented_or_missing_quote",
                                "supported_without_evidence",
                                "cited_page_lacks_support_quote"}
    def persist(result: dict) -> None:
        if not cache_path:
            return
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = cache_path.with_name(cache_path.name + ".tmp")
        temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(cache_path)

    for offset in range(0, len(assertions), MAX_CLAIMS_PER_CALL):
        batch = assertions[offset:offset + MAX_CLAIMS_PER_CALL]
        payload = {"claims": batch, "neighbor_claims": assertions[max(0, offset - 2):offset]
                   + assertions[offset + len(batch):offset + len(batch) + 2],
                   "blocks": blocks, "pages": pages}
        user = json.dumps(payload, ensure_ascii=False)
        batch_signature = hashlib.sha256(json.dumps(
            {"policy": POLICY, "model": model, "system": system, "user": user},
            ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        saved = accepted_batches.get(batch_signature)
        pending_repair_rows = None
        if saved is None and isinstance(cached.get("verdicts"), list):
            # Older partial manifests lacked per-batch checkpoints. Import
            # only a complete prefix batch after rechecking every quote.
            old_rows = cached["verdicts"][offset:offset + len(batch)]
            if len(old_rows) == len(batch):
                saved = {"verdicts": old_rows,
                         "relabeled_evidence": [row for row in cached.get(
                             "relabeled_evidence", []) if row.get("claim_id") in {
                                 claim["id"] for claim in batch}]}
        if isinstance(saved, dict):
            saved_rows = saved.get("verdicts")
            # A cache entry is usable only when the *whole* batch still passes
            # the same local schema, citation, and verbatim quote checks.
            cloned = json.loads(json.dumps(saved_rows, ensure_ascii=False))
            saved_issue, saved_issues, _ = _validate_batch(cloned, batch, evidence, pages)
            if not saved_issue and not saved_issues:
                verdicts.extend(cloned)
                old_relabels = saved.get("relabeled_evidence", [])
                if isinstance(old_relabels, list):
                    relabeled_evidence.extend(old_relabels)
                accepted_batches[batch_signature] = {
                    "verdicts": cloned,
                    "relabeled_evidence": old_relabels if isinstance(old_relabels, list) else []}
                continue
            if not saved_issue and any(row["reason"] in repairable_quote_reasons
                                       for row in saved_issues):
                pending_repair_rows = saved_rows
            accepted_batches.pop(batch_signature, None)
        if pending_repair_rows is None:
            raw = ""
            try:
                raw = ask(system, user)
                answer = _parse_or_repair_model_json(raw)
                if answer is None:
                    # Ask once for a fresh structured verdict; never scrape an
                    # arbitrary JSON object from a malformed response.
                    retry_payload = {**payload, "format_retry": {
                        "attempt": 1,
                        "instruction": ("上次回复不是单个合法 JSON 对象。请重新独立审核本批 claims，"
                                        "只返回一个 schema_version=1 的 JSON 对象，"
                                        "verdicts 数量与顺序必须和 claims 完全一致。"
                                        "不要代码围栏、解释、多个候选版本或重复整段回复。"
                                        "若原文引句包含英文双引号，必须按 JSON 转义，"
                                        "或另选不含双引号的连续短引句；不得改写原文。"
                                        "supported 引文仍须连续逐字且符合长度要求；"
                                        "不能因为格式重试而放宽证据标准。"),
                    }}
                    raw = ask(system, json.dumps(retry_payload, ensure_ascii=False))
                    answer = _parse_or_repair_model_json(raw)
                    if answer is None:
                        raise ValueError("invalid_json_after_format_retry")
            except (ValueError, TypeError) as exc:
                issue = f"invalid_json:{type(exc).__name__}"
                invalid_response_excerpt = str(raw)[:20000]
                break
            rows = answer.get("verdicts") if isinstance(answer, dict) else None
            if type(answer.get("schema_version")) is not int or answer["schema_version"] != 1:
                issue = "missing_or_misordered_verdict"
                break
        else:
            rows = json.loads(json.dumps(pending_repair_rows, ensure_ascii=False))
        original_rows = json.loads(json.dumps(rows, ensure_ascii=False))
        issue, batch_issues, batch_relabels = _validate_batch(rows, batch, evidence, pages)
        if issue in {"missing_or_misordered_verdict", "invalid_verdict_schema"}:
            expected_ids = [claim["id"] for claim in batch]
            schema_payload = {**payload, "schema_retry": {
                "attempt": 1,
                "previous_validation_issue": issue,
                "expected_ids_in_order": expected_ids,
                "instruction": ("上次事实编号或字段结构不合规。请独立重审本批，只返回"
                                "expected_ids_in_order 中的每一条，严格按所列顺序，"
                                "数量恰好相等；不得新增或省略。每个 verdict 仅允许"
                                "id、status、evidence、reason 四个字段；evidence 中"
                                "每项仅允许 id、quote 两个字符串字段。每条仍需完整事实判断，"
                                "supported 仍须逐字连续证据。只返回单个 JSON 对象。"),
            }}
            schema_raw = ""
            try:
                schema_raw = ask(system, json.dumps(schema_payload, ensure_ascii=False))
                try:
                    schema_answer = _parse_model_json(schema_raw)
                except (ValueError, TypeError):
                    schema_answer = _repair_reason_quotes(schema_raw)
                    if schema_answer is None:
                        raise
            except (ValueError, TypeError) as exc:
                issue = f"schema_retry_invalid_json:{type(exc).__name__}"
                invalid_response_excerpt = str(schema_raw)[:20000]
                break
            if schema_answer.get("schema_version") != 1:
                issue = "schema_retry_invalid_schema"
                break
            rows = schema_answer.get("verdicts")
            original_rows = json.loads(json.dumps(rows, ensure_ascii=False))
            issue, batch_issues, batch_relabels = _validate_batch(
                rows, batch, evidence, pages)
        if issue:
            break
        if batch_issues:
            # One supported claim can contain an extra fabricated quote beside
            # otherwise valid evidence. Ask for a fresh verdict using only the
            # valid subset and its original source text. Do not delete the bad
            # quote and accept the old verdict by implication.
            affected = {item["id"] for item in batch_issues}
            if len(affected) == 1 and all(
                item["reason"] == "invented_or_missing_quote" for item in batch_issues
            ):
                target_id = next(iter(affected))
                position = next(i for i, claim in enumerate(batch)
                                if claim["id"] == target_id)
                valid_quotes = _valid_quote_subset(
                    batch[position], rows[position], evidence, pages)
                if valid_quotes is not None:
                    subset_ids = {item["id"] for item in valid_quotes}
                    subset_payload = {
                        "claims": [batch[position]],
                        "valid_quote_subset_review": {
                            "attempt": 1,
                            "instruction": (
                                "独立重新判断这条完整 claim 是否获下列有效引句及其原始证据明确支持。"
                                "之前的 supported 判定含一条无效引句，不能沿用。"
                                "只可引用列出的证据 ID；若有效引句不足以证明整句，"
                                "判 unsupported 或 uncertain。必须重新给出逐字证据引句。"
                            ),
                            "valid_quotes": valid_quotes,
                            "original_evidence": [
                                {"id": identity, "text": evidence[identity]}
                                for identity in sorted(subset_ids)],
                        },
                    }
                    subset_raw = ""
                    subset_answer: dict = {}
                    try:
                        subset_raw = ask(system, json.dumps(subset_payload, ensure_ascii=False))
                        try:
                            subset_answer = _parse_model_json(subset_raw)
                        except (ValueError, TypeError):
                            subset_answer = _repair_reason_quotes(subset_raw)
                            if subset_answer is None:
                                raise
                    except (ValueError, TypeError) as exc:
                        issue = ""
                        invalid_response_excerpt = str(subset_raw)[:20000]
                        validation_issues.extend(batch_issues)
                        # Keep the original batch issues so the focused
                        # evidence-ID retry below can handle quote-heavy
                        # claims without weakening the evidence standard.
                    if (type(subset_answer.get("schema_version")) is not int
                            or subset_answer["schema_version"] != 1):
                        issue = ""
                        validation_issues.extend(batch_issues)
                        subset_answer = {}
                    subset_rows = subset_answer.get("verdicts")
                    if (isinstance(subset_rows, list) and len(subset_rows) == 1
                            and isinstance(subset_rows[0], dict)
                            and isinstance(subset_rows[0].get("evidence"), list)
                            and any(not isinstance(item, dict) or item.get("id") not in subset_ids
                                    for item in subset_rows[0]["evidence"])):
                        issue = ""
                        validation_issues.extend(batch_issues)
                        subset_rows = None
                    subset_issue, subset_issues, _ = _validate_batch(
                        subset_rows, [batch[position]],
                        {identity: evidence[identity] for identity in subset_ids}, pages)
                    if subset_issue or subset_issues:
                        issue = ""
                        validation_issues.extend(batch_issues)
                        validation_issues.extend(subset_issues)
                    elif subset_rows:
                        rows[position] = subset_rows[0]
                        issue, batch_issues, batch_relabels = _validate_batch(
                            rows, batch, evidence, pages)
                        if issue:
                            break
        if batch_issues and any(row["reason"] in repairable_quote_reasons
                                for row in batch_issues):
            invalid_quote_details = []
            invalid_ids = {item["id"] for item in batch_issues}
            for prior in original_rows:
                if prior.get("id") not in invalid_ids:
                    continue
                for citation in prior.get("evidence", []):
                    if not isinstance(citation, dict):
                        continue
                    source_id = citation.get("id")
                    quote = citation.get("quote")
                    source_text = evidence.get(source_id, "")
                    if (isinstance(source_id, str) and isinstance(quote, str)
                            and (not _substantial_quote(quote, source_text)
                                 or _compact(quote) not in _compact(source_text))):
                        detail = {
                            "claim_id": prior["id"], "evidence_id": source_id,
                            "invalid_quote": quote,
                            "reason": ("too_short_after_removing_whitespace"
                                       if not _substantial_quote(quote, source_text)
                                       else "not_contiguous_in_source"),
                            "source_excerpt": source_text[:500],
                        }
                        if not _substantial_quote(quote, source_text):
                            position = source_text.find(quote)
                            if position >= 0:
                                detail["nearby_verbatim_source"] = source_text[
                                    max(0, position - 24):min(len(source_text),
                                                               position + len(quote) + 24)]
                        invalid_quote_details.append(detail)
            repair_payload = {**payload, "quote_repair": {
                "attempt": 1,
                "policy": "quote-repair-v2",
                "instruction": ("只修正下面批次的逐字证据引句与证据编号；每个 quote "
                                "必须是给定对应 block/page 中连续的原文子串，去除空白后至少六个字符；"
                                "含数字且至少三个汉字的完整标签允许五字。"
                                "invalid_quote_details 给出了不连续或编造的引句；"
                                "只复制同一来源中的一个连续短片段，不要把相隔的项目合成一句。"
                                "必须重新返回本批全部 claim 的严格 JSON verdicts；"
                                "若找不到支持整句的原文，就判 uncertain 或 unsupported。"
                                "不得改写证据、借用常识或把课件说成老师原话。"),
                "validation_issues": batch_issues,
                "invalid_quote_details": invalid_quote_details,
                "original_verdicts": original_rows,
            }}
            retry_raw = ""
            try:
                retry_raw = ask(system, json.dumps(repair_payload, ensure_ascii=False))
                try:
                    retry_answer = _parse_model_json(retry_raw)
                except (ValueError, TypeError):
                    retry_answer = _repair_reason_quotes(retry_raw)
                    if retry_answer is None:
                        format_payload = {**repair_payload, "format_retry": {
                            "attempt": 1,
                            "instruction": ("上次引文修复回复不是单个合法 JSON 对象。"
                                            "请重新独立审核本批全部 claims，只返回一个"
                                            "schema_version=1 的 JSON 对象，verdicts"
                                            " 数量与顺序必须一致。不要代码围栏、重复结尾"
                                            "括号或解释；逐字引文标准不变。"),
                        }}
                        retry_raw = ask(system, json.dumps(format_payload,
                                                              ensure_ascii=False))
                        try:
                            retry_answer = _parse_model_json(retry_raw)
                        except (ValueError, TypeError):
                            retry_answer = _repair_reason_quotes(retry_raw)
                            if retry_answer is None:
                                raise
            except (ValueError, TypeError) as exc:
                issue = f"quote_repair_invalid_json:{type(exc).__name__}"
                invalid_response_excerpt = str(retry_raw)[:20000]
                validation_issues.extend(batch_issues)
                break
            if type(retry_answer.get("schema_version")) is not int or retry_answer["schema_version"] != 1:
                issue = "quote_repair_invalid_schema"
                validation_issues.extend(batch_issues)
                break
            rows = retry_answer.get("verdicts")
            issue, batch_issues, batch_relabels = _validate_batch(rows, batch, evidence, pages)
            if issue:
                issue = f"quote_repair_{issue}"
                break
        if batch_issues and all(item["reason"] in repairable_quote_reasons
                                for item in batch_issues):
            # A four-claim repair can keep stitching slide lines together.
            # Re-evaluate only affected claims one at a time, then run the
            # ordinary whole-batch validator again. No invalid quote is ever
            # silently removed or treated as a positive verdict.
            focused_failed = False
            for claim_id in dict.fromkeys(item["id"] for item in batch_issues):
                position = next(i for i, claim in enumerate(batch)
                                if claim["id"] == claim_id)
                focused_payload = {
                    "claims": [batch[position]], "blocks": blocks, "pages": pages,
                    "focused_evidence_retry": {
                        "attempt": 1,
                        "instruction": ("只独立审核这一条完整事实。上一轮引文有拼接、遗漏或过短，"
                                        "不能沿用旧 verdict。逐字复制一个来源中连续的片段；"
                                        "若原文不能支持整句，就判 unsupported 或 uncertain。"
                                        "只返回一个合法 JSON verdict。"),
                        "previous_validation_issues": [
                            item for item in batch_issues if item["id"] == claim_id],
                    },
                }
                focused_raw = ""
                try:
                    focused_raw = ask(system, json.dumps(focused_payload, ensure_ascii=False))
                    try:
                        focused_answer = _parse_model_json(focused_raw)
                    except (ValueError, TypeError):
                        focused_answer = _repair_reason_quotes(focused_raw)
                        if focused_answer is None:
                            raise
                except (ValueError, TypeError) as exc:
                    issue = f"focused_retry_invalid_json:{type(exc).__name__}"
                    invalid_response_excerpt = str(focused_raw)[:20000]
                    focused_failed = True
                    break
                if (type(focused_answer.get("schema_version")) is not int
                        or focused_answer["schema_version"] != 1):
                    issue = "focused_retry_invalid_schema"
                    focused_failed = True
                    break
                focused_rows = focused_answer.get("verdicts")
                focused_issue, focused_issues, _ = _validate_batch(
                    focused_rows, [batch[position]], evidence, pages)
                if focused_issue or focused_issues:
                    snippet_diagnostic: list[str] = []
                    snippet_row = _review_with_snippet_ids(
                        batch[position], evidence_blocks, source_pages, ask,
                        diagnostic=snippet_diagnostic)
                    if snippet_row is not None:
                        snippet_issue, snippet_issues, _ = _validate_batch(
                            [snippet_row], [batch[position]], evidence, pages)
                        if not snippet_issue and not snippet_issues:
                            rows[position] = snippet_row
                            continue
                    issue = f"focused_retry_{focused_issue or 'invalid_evidence'}"
                    if snippet_diagnostic:
                        invalid_response_excerpt = "snippet_id_retry:\n" + snippet_diagnostic[-1]
                    validation_issues.extend(item for item in focused_issues
                                             if item not in batch_issues)
                    focused_failed = True
                    break
                rows[position] = focused_rows[0]
            if focused_failed:
                validation_issues.extend(batch_issues)
                break
            issue, batch_issues, batch_relabels = _validate_batch(
                rows, batch, evidence, pages)
            if issue:
                break
        if not batch_issues:
            slide_candidates = _slide_fact_review_candidates(batch, rows, pages)
            if slide_candidates:
                original_slide_rows = json.loads(json.dumps(rows, ensure_ascii=False))
                slide_payload = {**payload, "slide_fact_review": {
                    "attempt": 1,
                    "instruction": (
                        "只复核列出的被拒 claim 是否由它明确引用的课件页支持。"
                        "没有归属老师口述的课件示例，可以由该页逐字内容支持，"
                        "无需转写重复讲出；但课件存在或字面相似并不自动证明整句、"
                        "变换关系与数字都正确。老师口述、要求、时间、作业归属仍必须"
                        "有转写证据。重新返回本批全部 claim 的严格 JSON verdicts；"
                        "supported 必须带对应证据 ID 的连续逐字引句，"
                        "无法证明全部关系时保持 unsupported 或 uncertain。"),
                    "candidates": slide_candidates,
                    "original_verdicts": original_slide_rows,
                }}
                slide_raw = ""
                try:
                    slide_raw = ask(system, json.dumps(slide_payload, ensure_ascii=False))
                    try:
                        slide_answer = _parse_model_json(slide_raw)
                    except (ValueError, TypeError):
                        slide_answer = _repair_reason_quotes(slide_raw)
                        if slide_answer is None:
                            retry_payload = {**slide_payload, "format_retry": {
                                "attempt": 1,
                                "instruction": ("上次课件复核回复不是单个合法 JSON 对象。"
                                                "请重新独立审核同一批 claims，只返回一个"
                                                "schema_version=1 的 JSON 对象，verdicts"
                                                " 必须与 claims 数量、编号及顺序完全一致。"
                                                "不要代码围栏、附加右括号或解释；"
                                                "supported 的证据标准不变。"),
                            }}
                            slide_raw = ask(system, json.dumps(retry_payload,
                                                                 ensure_ascii=False))
                            try:
                                slide_answer = _parse_model_json(slide_raw)
                            except (ValueError, TypeError):
                                slide_answer = _repair_reason_quotes(slide_raw)
                                if slide_answer is None:
                                    raise
                except (ValueError, TypeError) as exc:
                    issue = f"slide_review_invalid_json:{type(exc).__name__}"
                    invalid_response_excerpt = str(slide_raw)[:20000]
                    break
                if type(slide_answer.get("schema_version")) is not int or slide_answer["schema_version"] != 1:
                    issue = "slide_review_invalid_schema"
                    break
                slide_rows = slide_answer.get("verdicts")
                slide_issue, slide_issues, slide_relabels = _validate_batch(
                    slide_rows, batch, evidence, pages)
                if slide_issue:
                    issue = f"slide_review_{slide_issue}"
                    break
                candidate_ids = {item["id"] for item in slide_candidates}
                for old, revised in zip(original_slide_rows, slide_rows):
                    if (old["id"] not in candidate_ids
                            and old["status"] != "supported"
                            and revised["status"] == "supported"):
                        slide_issues.append({"id": old["id"],
                                             "reason": "slide_review_unlisted_claim_promoted"})
                rows, batch_issues, batch_relabels = slide_rows, slide_issues, slide_relabels
        if not batch_issues:
            prior_relabels = list(batch_relabels)
            for position, (claim, row) in enumerate(zip(batch, rows)):
                if row["status"] == "supported":
                    continue
                verified = _verified_caesar_slide_example(claim, pages)
                if verified is not None:
                    rows[position] = verified
            issue, batch_issues, batch_relabels = _validate_batch(
                rows, batch, evidence, pages)
            if not issue and not batch_issues:
                current_by_id = {row["id"]: row for row in rows}
                for relabel in prior_relabels:
                    current = current_by_id.get(relabel["claim_id"], {})
                    if any(citation.get("id") == relabel["to"]
                           for citation in current.get("evidence", [])):
                        batch_relabels.append(relabel)
            if issue:
                break
        verdicts.extend(rows)
        validation_issues.extend(batch_issues)
        relabeled_evidence.extend(batch_relabels)
        if batch_issues:
            break
        accepted_batches[batch_signature] = {"verdicts": rows,
                                             "relabeled_evidence": batch_relabels}
        # Checkpoint each paid successful batch, including a valid negative
        # verdict, before asking the service for the next one.
        persist({"signature": signature, "status": "unverified",
                 "reason": "audit_in_progress", "accepted_batches": accepted_batches,
                 "assertions": assertions, "verdicts": verdicts})
    failed_claim = any(row["status"] != "supported" for row in verdicts)
    result = {"signature": signature,
              "status": ("fail" if failed_claim else
                         "unverified" if issue or validation_issues
                         or len(verdicts) != len(assertions) else "pass"),
              "reason": issue or ("invalid_evidence_quote" if validation_issues else ""),
              "invalid_response_excerpt": invalid_response_excerpt,
              "validation_issues": validation_issues,
              "relabeled_evidence": relabeled_evidence,
              "accepted_batches": accepted_batches,
              "assertions": assertions, "verdicts": verdicts}
    persist(result)
    return result
