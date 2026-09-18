"""课堂录像笔记 hub line parsing (VAL-META-006).

Verified line form: 《第N讲 · 课程（date 第X-Y节）》→ <url or page mention> —
the ONLY explicit lecture-content link channel today (the batch runbook
appends one line per new lecture page, so the format is stable). Both
verified target forms are parsed: a notion.so URL in the text and a native
page mention in the block's rich text. Body content (week-1 subpages embed
full transcript previews) is never captured: only lines that carry a
resolvable lecture-page target become entries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .entities import normalize_page_id
from .titles import normalize_title, parse_lecture_title

_LINE_TITLE_RE = re.compile(r"[《「『](?P<inner>[^《》「」『』]+)[》」』]")
_URL_TOKEN_RE = re.compile(r"https?://\S*notion\.so/\S+")
_HEX32_RE = re.compile(r"([0-9a-fA-F]{32})")


@dataclass(frozen=True)
class NoteLine:
    title: str
    number: int
    date: str
    period: str
    target_id: str  # normalized dashed id of the linked lecture page


def parse_note_lines(blocks: list[dict]) -> tuple[list[NoteLine], int]:
    """Parse a subpage's blocks into link lines.

    Returns (lines, skipped_count). A lecture-titled 《…》 line whose target
    does not resolve is skipped and counted (diagnostics), never captured.
    """
    lines: list[NoteLine] = []
    skipped = 0
    for block in blocks:
        rich = _rich_text(block)
        if not rich:
            continue
        line = _parse_one(rich)
        if line is None:
            if _has_lecture_titled_span(rich):
                skipped += 1
            continue
        lines.append(line)
    return lines, skipped


def _rich_text(block: dict) -> list[dict] | None:
    inner = block.get(block.get("type", "")) or {}
    rich = inner.get("rich_text")
    return rich if rich else None


def _parse_one(rich: list[dict]) -> NoteLine | None:
    text = "".join(piece.get("plain_text", "") for piece in rich)
    for match in _LINE_TITLE_RE.finditer(text):
        inner = normalize_title(match.group("inner"))
        parsed = parse_lecture_title(inner)
        if parsed is None:
            continue  # a stray non-lecture 《…》 quote before the real line
        target = _target_id(rich, text)
        if target is None:
            return None
        return NoteLine(
            title=inner,
            number=parsed.number,
            date=parsed.date,
            period=parsed.period,
            target_id=target,
        )
    return None


def _has_lecture_titled_span(rich: list[dict]) -> bool:
    text = "".join(piece.get("plain_text", "") for piece in rich)
    return any(
        parse_lecture_title(normalize_title(match.group("inner"))) is not None
        for match in _LINE_TITLE_RE.finditer(text)
    )


def _target_id(rich: list[dict], text: str) -> str | None:
    """Structured page mention first, then the first notion.so URL."""
    for piece in rich:
        mention = piece.get("mention") or {}
        if mention.get("type") == "page":
            page_id = (mention.get("page") or {}).get("id")
            if page_id:
                return normalize_page_id(page_id)
    for token in _URL_TOKEN_RE.findall(text):
        for hex_id in _HEX32_RE.findall(token):
            return normalize_page_id(hex_id)
    return None
