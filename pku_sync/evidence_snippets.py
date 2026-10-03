"""Deterministic, source-bound quote choices for a lecture claim auditor.

The catalog only supplies possible evidence. A model must still decide whether
the selected snippets support every part of a claim. Ordinary snippets never
join PDF lines. A support bundle is different: it is one exact contiguous span
across nearby source lines, retained so diagrams and short labels can be
reviewed together without letting the model author a synthetic quotation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable


_HANZI = re.compile(r"[\u4e00-\u9fff]")
_LINE = re.compile(r"[^\r\n]+")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _eligible(value: str) -> bool:
    """Keep brief slide labels that an ID can verify without model rewriting."""
    compact = _compact(value)
    return len(compact) >= 6 or (len(compact) >= 5
                                 and any(char.isdigit() for char in compact)
                                 and len(_HANZI.findall(compact)) >= 3) or (
                                     len(_HANZI.findall(compact)) >= 4)


def _source_rows(evidence_blocks: list[dict], source_pages: list[dict]) -> list[dict]:
    """Use the same text ceilings and page numbering as ``audit_chapter``."""
    rows: list[dict] = []
    for block in evidence_blocks:
        if block.get("block_id") and block.get("text"):
            rows.append({"source_id": str(block["block_id"]),
                         "source_kind": "transcript", "text": str(block["text"])[:220],
                         "title": "", "locator": "", "status": ""})
    pages = []
    for index, page in enumerate(source_pages[:8], 1):
        if page.get("text"):
            pages.append({"source_id": f"S{index}", "source_kind": "page",
                          "text": str(page["text"])[:2500],
                          "title": str(page.get("title") or ""),
                          "locator": str(page.get("locator") or ""),
                          "status": str(page.get("status") or "")})
    remaining = 12000
    for page in pages:
        page["text"] = page["text"][:remaining]
        remaining -= len(page["text"])
    return [*rows, *pages]


def _spans(text: str, max_snippet_chars: int) -> Iterable[tuple[int, int]]:
    """Emit whole short lines, or contiguous chunks within an individual line."""
    for match in _LINE.finditer(text):
        line_start, line_end = match.span()
        while line_start < line_end and text[line_start].isspace():
            line_start += 1
        while line_end > line_start and text[line_end - 1].isspace():
            line_end -= 1
        start = line_start
        while start < line_end:
            limit = min(start + max_snippet_chars, line_end)
            end = limit
            if limit < line_end:
                # Keep a sentence or phrase together when possible. The chosen
                # boundary remains within this *one* original source line.
                window = text[start:limit]
                breaks = list(re.finditer(r"[。！？!?；;，,:：、]\s*|\s+", window))
                usable = [item.end() for item in breaks
                          if item.end() >= max_snippet_chars // 2]
                if usable:
                    end = start + usable[-1]
            while end > start and text[end - 1].isspace():
                end -= 1
            if end > start:
                yield start, end
            start = max(limit if end <= start else end, start + 1)
            while start < line_end and text[start].isspace():
                start += 1


def _line_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in _LINE.finditer(text):
        start, end = match.span()
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            spans.append((start, end))
    return spans


def _support_bundles(source: dict, *, max_chars: int = 120) -> list[dict]:
    """Build exact contiguous spans for nearby short labels on one source."""
    lines = _line_spans(source["text"])
    bundles = []
    seen: set[tuple[int, int]] = set()
    for first in range(len(lines)):
        for count in range(2, min(4, len(lines) - first) + 1):
            start, _ = lines[first]
            _, end = lines[first + count - 1]
            quote = source["text"][start:end]
            if len(_compact(quote)) > max_chars or not _eligible(quote):
                continue
            identity = (start, end)
            if identity in seen:
                continue
            seen.add(identity)
            digest = hashlib.sha256(json.dumps(
                ("bundle", source["source_kind"], source["source_id"],
                 source["title"], source["locator"], start, end),
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()[:16]
            bundles.append({
                "id": f"G{digest}",
                "source_id": source["source_id"],
                "source_kind": source["source_kind"],
                "title": source["title"],
                "locator": source["locator"],
                "status": source["status"],
                "start": start,
                "end": end,
                "quote": quote,
                "members": [
                    source["text"][member_start:member_end]
                    for member_start, member_end in lines[first:first + count]
                ],
                "line_count": count,
            })
    return bundles


def _query_score(row: dict, query: str) -> int:
    if not query:
        return 0
    target = _compact(query).casefold()
    quote = _compact(row["quote"]).casefold()
    pairs = {target[index:index + 2] for index in range(len(target) - 1)}
    score = sum(pair in quote for pair in pairs)
    if row["source_kind"] == "page":
        label = _compact(row["title"] + row["locator"]).casefold()
        if row["title"] and _compact(row["title"]).casefold() in target:
            score += 6
        if row["locator"] and _compact(row["locator"]).casefold() in target:
            score += 8
        if label and label in target:
            score += 4
    return score


def build_evidence_snippet_catalog(
    evidence_blocks: list[dict],
    source_pages: list[dict],
    *,
    query: str = "",
    max_snippet_chars: int = 160,
    max_payload_chars: int = 12000,
    max_snippets: int = 120,
) -> dict:
    """Return bounded quote choices with exact source offsets and stable IDs.

    ``query`` can be the current batch's claims. It only ranks which excerpts
    fit a bounded prompt; it never changes the IDs or the quote contents.
    ``truncated`` means there are eligible source excerpts outside the catalog.
    """
    if max_snippet_chars < 16 or max_payload_chars < 256 or max_snippets < 1:
        raise ValueError("catalog limits too small")
    candidates: list[dict] = []
    bundle_candidates: list[dict] = []
    seen_ids: set[str] = set()
    for source in _source_rows(evidence_blocks, source_pages):
        bundle_candidates.extend(_support_bundles(source))
        for start, end in _spans(source["text"], max_snippet_chars):
            quote = source["text"][start:end]
            if not _eligible(quote):
                continue
            identity = (source["source_kind"], source["source_id"],
                        source["title"], source["locator"], start, end, quote)
            digest = hashlib.sha256(json.dumps(
                identity, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")).hexdigest()[:16]
            snippet_id = f"E{digest}"
            if snippet_id in seen_ids:
                raise ValueError("snippet ID collision or duplicate source ID")
            seen_ids.add(snippet_id)
            candidates.append({"id": snippet_id, "source_id": source["source_id"],
                               "source_kind": source["source_kind"],
                               "title": source["title"], "locator": source["locator"],
                               "status": source["status"], "start": start, "end": end,
                               "quote": quote})
    ranked = sorted(enumerate(candidates),
                    key=lambda item: (-_query_score(item[1], query), item[0]))
    ranked_bundles = sorted(
        enumerate(bundle_candidates),
        key=lambda item: (-_query_score(item[1], query), item[0]),
    )
    bundle_budget = min(3000, max_payload_chars // 3)
    bundles: list[dict] = []
    bundle_chars = 2
    for _, row in ranked_bundles:
        size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        if len(bundles) >= 24 or bundle_chars + size + bool(bundles) > bundle_budget:
            continue
        bundles.append(row)
        bundle_chars += size + bool(len(bundles) > 1)

    selected: list[tuple[int, dict]] = []
    payload_chars = 2
    snippet_budget = max(256, max_payload_chars - bundle_chars - 32)
    for ordinal, row in ranked:
        size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        if (len(selected) >= max_snippets
                or payload_chars + size + bool(selected) > snippet_budget):
            continue
        selected.append((ordinal, row))
        payload_chars += size + bool(len(selected) > 1)
    selected.sort(key=lambda item: item[0])
    snippets = [row for _, row in selected]
    payload = {"snippets": snippets, "support_bundles": bundles}
    payload_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return {"snippets": snippets, "support_bundles": bundles,
            "truncated": (len(snippets) != len(candidates)
                          or len(bundles) != len(bundle_candidates)),
            "eligible_count": len(candidates),
            "bundle_count": len(bundles),
            "payload_chars": payload_size}


def resolve_snippet_ids(catalog: dict, snippet_ids: list[str]) -> list[dict[str, str]]:
    """Map only listed IDs to immutable source IDs and exact quote strings."""
    by_id = {row["id"]: row for row in catalog["snippets"]}
    if len(snippet_ids) != len(set(snippet_ids)):
        raise ValueError("duplicate snippet ID")
    try:
        return [{"id": by_id[identity]["source_id"],
                 "quote": by_id[identity]["quote"]} for identity in snippet_ids]
    except KeyError as exc:
        raise ValueError("unknown snippet ID") from exc


def resolve_evidence_choice_ids(
    catalog: dict, choice_ids: list[str],
) -> list[dict[str, str]]:
    """Resolve ordinary snippets or exact contiguous support bundles."""
    snippets = {row["id"]: row for row in catalog.get("snippets", [])}
    bundles = {row["id"]: row for row in catalog.get("support_bundles", [])}
    if len(choice_ids) != len(set(choice_ids)):
        raise ValueError("duplicate evidence choice ID")
    result = []
    for identity in choice_ids:
        row = snippets.get(identity) or bundles.get(identity)
        if row is None:
            raise ValueError("unknown evidence choice ID")
        result.append({"id": row["source_id"], "quote": row["quote"]})
    return result
