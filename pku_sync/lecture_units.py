"""Structured teaching-unit packets for course-note generation.

The note writer should receive a small model of one teaching unit, not a flat
bag of transcript text and PDF excerpts.  This module is deterministic: it
does not decide whether a claim is true and it does not call an LLM.  It only
preserves the provenance and coverage signals already established upstream.
"""

from __future__ import annotations

import json
import re
from typing import Any


SCHEMA_VERSION = 1
MAX_BLOCKS = 48
MAX_SLIDES = 8
MAX_SLIDE_TEXT = 2500


def _time_label(seconds: Any) -> str:
    try:
        value = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        value = 0
    return f"[{value // 60:02d}:{value % 60:02d}]"


def _page_key(row: dict[str, Any]) -> tuple[str, str]:
    locator = str(row.get("locator") or "")
    match = re.fullmatch(r"第\s*(\d+)\s*页", locator)
    if match:
        locator = f"第{int(match.group(1))}页"
    return (str(row.get("title") or ""), locator)


def _slide_row(row: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "filename": str(row.get("title") or ""),
        "page": str(row.get("locator") or ""),
        "status": str(row.get("status") or "readable"),
        "has_extracted_text": bool(str(row.get("text") or "").strip()),
    }
    if confirmed:
        result["confirmed_on_screen"] = True
    elif row.get("projected"):
        result["confirmed_on_screen"] = True
    return result


def build_unit_packet(
    index: int,
    window: dict[str, Any],
    projected: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one bounded, provenance-preserving teaching-unit packet."""
    confirmed_keys = {
        (str(row.get("title") or ""), f"第{int(row.get('page') or 0)}页")
        for row in projected
        if row.get("certain") and int(row.get("page") or 0) > 0
    }
    confirmed: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in sources:
        key = _page_key(row)
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        is_confirmed = key in confirmed_keys
        item = _slide_row(row, confirmed=is_confirmed)
        if is_confirmed:
            confirmed.append(item)
        candidates.append(item)
    candidates.sort(
        key=lambda row: (not row.get("confirmed_on_screen", False),
                         row["filename"], row["page"])
    )
    blocks = []
    for block in (window.get("evidence_blocks") or [])[:MAX_BLOCKS]:
        if not isinstance(block, dict) or not block.get("text"):
            continue
        blocks.append({
            "id": str(block.get("block_id") or ""),
            "start": block.get("start"),
            "end": block.get("end"),
            "time_label": _time_label(block.get("start")),
            "text": str(block.get("text") or ""),
        })
    transcript = str(window.get("text") or "")
    plain_transcript = re.sub(r"\[\d{1,3}:\d{2}\]", "", transcript)
    return {
        "schema_version": SCHEMA_VERSION,
        "unit_id": f"U{index + 1:04d}",
        "time": {
            "start": float(window.get("start") or 0),
            "end": float(window.get("end") or 0),
        },
        "transcript": {
            "characters": len(plain_transcript),
            "evidence_blocks": blocks,
        },
        "slides": {
            "confirmed_on_screen": confirmed[:MAX_SLIDES],
            "candidates": candidates[:MAX_SLIDES],
        },
        "coverage": {
            "transcript_characters": len(plain_transcript),
            "evidence_block_count": len(blocks),
            "confirmed_slide_count": len(confirmed),
            "candidate_slide_count": min(len(candidates), MAX_SLIDES),
            "has_slide_evidence": bool(confirmed or candidates),
        },
        "writing_policy": {
            "transcript_authority": "speech_examples_coursework",
            "slide_authority": "printed_definitions_numbers_formulas",
            "slide_is_not_speech": True,
            "page_existence_is_not_sentence_support": True,
            "absence_of_cue_is_not_no_coursework": True,
        },
    }


def format_unit_context(packet: dict[str, Any]) -> str:
    """Serialize a unit packet for a model prompt and cache fingerprint."""
    return json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
