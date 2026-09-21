"""Strict single-fence and bounded-prose tolerance for metered LLM JSON.

The panel's strict JSON gates (the organizer's ``_parse_quiz`` and the
grader's ``pku-e2e-grade-v1`` contract validation) parse organize/grade
relay responses with ``json.loads``: any wrapper around the JSON object
burns the already-metered operation (the Window D first real run rejected a
fenced Bailian quiz response, and the Window C first run burned a charged
quiz op on surrounding prose). Two tolerance layers share this module:

- ``unwrap_single_json_fence`` — the pinned, unchanged semantics: the one
  tolerated wrapper is exactly one Markdown code fence, with nothing but
  whitespace around it. Everything else passes through untouched.
- ``extract_single_json_payload`` — the shared bounded recovery used by the
  gates before a metered operation is declared lost: valid JSON parses as-is
  (the gates keep their own shape checks), then one complete json fence
  unwraps with the pinned fence rules, then bounded non-object text around
  EXACTLY ONE JSON object is tolerated — leading prose and trailing
  commentary, raw or fenced, CRLF variants included.

Still rejected with the gates' unchanged reasons: multiple JSON objects or
fences, nested fences, a stray embedded example object alongside the real
payload, prose with no object, non-object JSON top levels, a non-json fence
tag, an unterminated fence, wrapper text beyond ``MAX_WRAPPER_CHARS``, and
any contract-invalid content inside the recovered object.
"""
from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["unwrap_single_json_fence", "extract_single_json_payload", "MAX_WRAPPER_CHARS"]

# Bounded non-object text tolerated around the single JSON object.
MAX_WRAPPER_CHARS = 2048
_UNRECOVERABLE = "no single recoverable JSON object in the response"

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


def _payload_regions(content: str) -> list[str] | None:
    """Return the non-fence scan regions, or None for an invalid fence shape.

    With no fence line at all the whole content is one region. With fences
    present the pinned ``unwrap_single_json_fence`` rules apply: exactly one
    complete json-or-empty-tagged fence pair is allowed and the regions are
    the prose before the fence, the payload between the fence lines, and the
    prose after it. Anything else — a second or nested fence, an unterminated
    fence, or a non-json info tag — is structurally invalid and recovery
    refuses the content (a naive prose scan must not fish the object out of
    an ambiguous fence).
    """
    lines = content.split("\n")
    fence_indexes = [
        index
        for index, line in enumerate(lines)
        if _FENCE_LINE_RE.match(line.rstrip("\r")) is not None
    ]
    if not fence_indexes:
        return [content]
    if len(fence_indexes) != 2:
        return None
    open_index, close_index = fence_indexes
    open_match = _FENCE_LINE_RE.match(lines[open_index].rstrip("\r"))
    info = open_match.group(2)
    if info and re.fullmatch(r"json", info, re.IGNORECASE) is None:
        return None
    if not _is_closing_fence(lines[close_index].rstrip("\r"), open_match.group(1)):
        return None
    return [
        "\n".join(lines[:open_index]),
        "\n".join(lines[open_index + 1:close_index]),
        "\n".join(lines[close_index + 1:]),
    ]


def _matching_brace(text: str, start: int) -> int | None:
    """Index of the ``}`` closing the ``{`` at ``start``, string-aware."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _container_depth(prefix: str) -> int:
    """Net JSON container depth of the text before a candidate, string-aware.

    A positive depth means the position sits inside an unclosed ``[``/``{``
    container (e.g. the element of a top-level array); balanced brackets in
    prose net out to zero. Only ``[``/``{``/``]``/``}`` outside string
    literals count.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in prefix:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return depth


def _object_candidates(text: str) -> list[tuple[dict, int, int]]:
    """Left-to-right top-level JSON object candidates in one region.

    A braced span only counts when it parses as a JSON object AND sits at
    container depth zero, so prose braces (format hints like ``{title,
    questions:[...]}``) never masquerade as candidates, nested objects stay
    inside their consumed parent, and an object that is merely an element
    of a top-level array (a non-object top level) is not recovered.
    """
    candidates: list[tuple[dict, int, int]] = []
    position = 0
    while True:
        start = text.find("{", position)
        if start < 0:
            return candidates
        end = _matching_brace(text, start)
        if end is not None:
            try:
                value = json.loads(text[start:end + 1])
            except ValueError:
                value = None
            if isinstance(value, dict):
                if _container_depth(text[:start]) <= 0:
                    candidates.append((value, start, end + 1))
                position = end + 1
                continue
        position = start + 1


def extract_single_json_payload(content: Any) -> Any:
    """Recover the single JSON object from a metered LLM response, if any.

    Returns the parsed value for shapes the gates tolerate; raises
    ``ValueError`` when nothing is recoverable so every gate rejects with
    its unchanged reason. Non-string content passes through untouched and
    plain JSON parses as-is (any top level — the gates keep their own
    shape checks), so today's behavior for those shapes is byte-identical.
    A fence is unwrapped only under the pinned ``unwrap_single_json_fence``
    rules, and the prose around the one recovered object must stay within
    ``MAX_WRAPPER_CHARS``.
    """
    if not isinstance(content, str):
        return content
    try:
        return json.loads(content)
    except ValueError:
        pass
    regions = _payload_regions(content)
    if regions is None:
        raise ValueError(_UNRECOVERABLE)
    candidates: list[tuple[dict, int, int]] = []
    for region in regions:
        candidates.extend(_object_candidates(region))
    if len(candidates) != 1:
        raise ValueError(_UNRECOVERABLE)
    value, start, end = candidates[0]
    if len(content) - (end - start) > MAX_WRAPPER_CHARS:
        raise ValueError(_UNRECOVERABLE)
    return value
