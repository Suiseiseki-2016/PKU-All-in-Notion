"""Strict single-fence tolerance for metered LLM JSON responses.

The panel's strict JSON gates (the organizer's ``_parse_quiz`` and the
grader's ``pku-e2e-grade-v1`` contract validation) parse organize/grade
relay responses with ``json.loads``: any wrapper around the JSON object
burns the already-metered operation (the Window D first real run rejected a
fenced Bailian quiz response after the relay charge). The one tolerated
wrapper is exactly one Markdown code fence — the entire response may be
nothing but whitespace around that single fence. Everything else passes
through untouched so the existing schema/type/identity/range validation
rejects it exactly as before.
"""
from __future__ import annotations

import re
from typing import Any

__all__ = ["unwrap_single_json_fence"]

# A fence line is three or more backticks or tildes with an optional info
# string; only an empty or json-tagged (case-insensitive) info string is an
# equivalent JSON fence.
_FENCE_LINE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([^ \t]*)[ \t]*$")


def _is_closing_fence(line: str, fence: str) -> bool:
    """A closing fence is the same char, at least as long, with no info."""
    match = _FENCE_LINE_RE.match(line)
    return (
        match is not None
        and match.group(2) == ""
        and match.group(1)[0] == fence[0]
        and len(match.group(1)) >= len(fence)
    )


def unwrap_single_json_fence(content: Any) -> Any:
    """Return the payload inside exactly one fenced wrapper, else the content.

    The response may only be whitespace surrounding one complete Markdown
    code fence (```json, ```JSON, an untagged ```, or the tilde equivalents).
    The payload between the fence lines is returned verbatim. Every other
    shape — a second fence, a nested fence, surrounding prose, a non-json
    info tag, or a missing closing fence — returns the content untouched so
    the strict gate rejects it unchanged.
    """
    if not isinstance(content, str):
        return content
    lines = content.split("\n")
    open_index = next(
        (index for index, line in enumerate(lines) if line.strip()), None
    )
    if open_index is None:
        return content
    match = _FENCE_LINE_RE.match(lines[open_index].rstrip("\r"))
    info = match.group(2) if match is not None else ""
    if match is None or (info and re.fullmatch(r"json", info, re.IGNORECASE) is None):
        return content
    fence = match.group(1)
    close_index = next(
        (
            index
            for index in range(open_index + 1, len(lines))
            if _is_closing_fence(lines[index].rstrip("\r"), fence)
        ),
        None,
    )
    if close_index is None:
        return content
    # Only whitespace may follow the single closing fence: a second or
    # nested fence leaves real content behind and is not unwrapped.
    if any(line.strip() for line in lines[close_index + 1:]):
        return content
    return "\n".join(lines[open_index + 1:close_index])
