"""Conservative, one-call revision of failed chapter claims.

This module produces an *unverified candidate*, never a passing audit.  A
replacement is constructed from one contiguous source quote, rather than
trusting a model to paraphrase facts which cannot be checked locally.  The
candidate must be sent through ``claim_audit.audit_chapter`` again.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from .claim_audit import chapter_assertions


MAX_REVISIONS = 8
_SENSITIVE = re.compile(
    r"作业|提交|截止|考勤|签到|到场|出勤|展示|分组|考试|测验|"
    r"(?:https?://)|(?:\d{1,2}[:：]\d{2})|"
    r"(?:\d+\s*(?:mod|模|分钟|学分))|(?:[=≡→])",
    re.I,
)
_PAGE_LOCATOR = re.compile(r"^第\s*(\d+)\s*页$")


def _refusal(reason: str) -> dict:
    return {"status": "refused", "reason": reason, "candidate": None,
            "changes": [], "requires_reaudit": True}


def _parse_response(raw: str) -> dict:
    value = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", value, re.I)
    if fence:
        value = fence.group(1)
    answer = json.loads(value)
    if not isinstance(answer, dict):
        raise ValueError("response_not_object")
    return answer


def _evidence_catalog(blocks: list[dict], pages: list[dict]) -> dict[str, dict]:
    catalog: dict[str, dict] = {}
    for row in blocks:
        identity = str(row.get("block_id") or "")
        if identity and row.get("text") and identity not in catalog:
            catalog[identity] = {"kind": "transcript", "text": str(row["text"])[:220]}
    remaining = 12000
    for index, row in enumerate(pages[:8], 1):
        identity = f"S{index}"
        if row.get("text") and identity not in catalog:
            content = str(row["text"])[:2500][:remaining]
            remaining -= len(content)
            if not content:
                continue
            catalog[identity] = {
                "kind": "slide", "text": content,
                "title": str(row.get("title") or ""),
                "locator": str(row.get("locator") or ""),
            }
    return catalog


def revise_failed_chapter(
    chapter: str,
    audit: dict,
    evidence_blocks: list[dict],
    source_pages: list[dict],
    ask: Callable[[str, str], str],
) -> dict:
    """Return a source-bound candidate or refuse without changing the note.

    The audit must be complete and bound to this exact chapter's assertions.
    Every failed claim must have exactly one replacement; supported claims stay
    byte-for-byte.  The model chooses a single verbatim quote for each failed
    claim.  Locally constructed replacements contain no generated factual
    prose.  Any unsafe response, ambiguous text span, or absent source refuses
    the entire revision.  The caller must re-audit the returned candidate.
    """
    if not isinstance(chapter, str) or not isinstance(audit, dict):
        return _refusal("invalid_input")
    assertions = chapter_assertions(chapter)
    prior = audit.get("assertions")
    verdicts = audit.get("verdicts")
    if (not assertions or prior != assertions or not isinstance(verdicts, list)
            or len(verdicts) != len(assertions)):
        return _refusal("incomplete_or_stale_audit")
    failed: list[dict] = []
    for claim, verdict in zip(assertions, verdicts):
        if not isinstance(verdict, dict) or verdict.get("id") != claim["id"]:
            return _refusal("incomplete_or_stale_audit")
        status = verdict.get("status")
        if status not in {"supported", "unsupported", "uncertain"}:
            return _refusal("invalid_verdict")
        if status != "supported":
            failed.append(claim)
    if not failed:
        return _refusal("no_failed_claims")
    if len(failed) > MAX_REVISIONS:
        return _refusal("revision_limit")
    if any(_SENSITIVE.search(claim["text"]) or re.search(
        r"\d", re.sub(r"（课件：[^）]+）", "", claim["text"]))
           for claim in failed):
        return _refusal("sensitive_claim_requires_manual_revision")
    for claim in failed:
        if chapter.count(claim["text"]) != 1:
            return _refusal("ambiguous_claim_span")

    catalog = _evidence_catalog(evidence_blocks, source_pages)
    if not catalog:
        return _refusal("missing_sources")
    system = (
        "你是课堂笔记修订助手。只为 failed_claims 每项选一段单一来源中的连续逐字原文，"
        "不要改写、拼接、引入常识或新增事实。选不出能准确替换原句的完整自然句子时，"
        "返回 refusal=true。不能修改已获支持的句子、作业、到场要求和例题步骤。"
        "严格返回一个 JSON 对象：{\"schema_version\":1,\"refusal\":false,"
        "\"revisions\":[{\"id\":\"C001\",\"source_id\":\"S1\","
        "\"quote\":\"同一来源中的连续逐字原文\"}]}。不附解释。"
    )
    payload = {
        "failed_claims": [{**claim, "reason": verdicts[int(claim["id"][1:]) - 1].get("reason", "")}
                          for claim in failed],
        "sources": [{"id": identity, **row} for identity, row in catalog.items()],
    }
    try:
        answer = _parse_response(ask(system, json.dumps(payload, ensure_ascii=False)))
    except (ValueError, TypeError, KeyError):
        return _refusal("invalid_model_response")
    if answer.get("schema_version") != 1 or type(answer.get("refusal")) is not bool:
        return _refusal("invalid_model_response")
    if answer["refusal"]:
        return _refusal("model_cannot_ground_revision")
    revisions = answer.get("revisions")
    if (not isinstance(revisions, list) or len(revisions) != len(failed)
            or [row.get("id") for row in revisions if isinstance(row, dict)]
            != [row["id"] for row in failed]):
        return _refusal("missing_or_misordered_revisions")

    candidate = chapter
    changes = []
    for claim, revision in zip(failed, revisions):
        if (not isinstance(revision, dict)
                or set(revision) != {"id", "source_id", "quote"}
                or not isinstance(revision["source_id"], str)
                or not isinstance(revision["quote"], str)):
            return _refusal("invalid_revision_schema")
        source = catalog.get(revision["source_id"])
        quote = revision["quote"].strip()
        if (not source or len(quote) < 8 or len(quote) > 240
                or quote not in source["text"] or "\n" in quote
                or not re.search(r"[。！？!?]$", quote)
                or len(re.findall(r"[。！？!?]", quote)) != 1):
            return _refusal("quote_not_contiguous_complete_sentence")
        # An explicit page citation in a failed claim may only be repaired
        # from that very page; another source cannot launder a wrong citation.
        old_citations = re.findall(r"（课件：([^）]+)）", claim["text"])
        if old_citations:
            if source["kind"] != "slide" or len(old_citations) != 1:
                return _refusal("cited_page_not_supplied")
            match = re.fullmatch(r"([^，；]+)，\s*第\s*(\d+)\s*页", old_citations[0])
            locator = _PAGE_LOCATOR.fullmatch(source["locator"])
            if (not match or not locator or match.group(1).strip() != source["title"]
                    or int(match.group(2)) != int(locator.group(1))):
                return _refusal("cited_page_not_supplied")
        if source["kind"] == "slide":
            locator = _PAGE_LOCATOR.fullmatch(source["locator"])
            if not source["title"] or not locator:
                return _refusal("unlocatable_slide")
            replacement = (f"{quote[:-1]}（课件：{source['title']}，"
                           f"第{int(locator.group(1))}页）{quote[-1]}")
        else:
            replacement = quote
        candidate = candidate.replace(claim["text"], replacement, 1)
        changes.append({"id": claim["id"], "source_id": revision["source_id"],
                        "old": claim["text"], "new": replacement})

    # Reparse so punctuation/layout changes cannot accidentally absorb or
    # remove an already supported assertion or a separate failed sentence.
    supported = [claim["text"] for claim, row in zip(assertions, verdicts)
                 if row["status"] == "supported"]
    new_claims = [claim["text"] for claim in chapter_assertions(candidate)]
    if any(new_claims.count(sentence) < supported.count(sentence)
           for sentence in set(supported)):
        return _refusal("supported_claim_changed")
    if len(new_claims) != len(assertions):
        return _refusal("assertion_count_changed")
    return {"status": "candidate", "reason": "requires_full_reaudit",
            "candidate": candidate, "changes": changes, "requires_reaudit": True}
