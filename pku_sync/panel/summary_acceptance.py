"""Explicit human acceptance tied to an exact official lecture and note bytes."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from ..catalog_snapshots import recording_source_id

_PREFIX = "PKU 总结验收版本 · "


def _marker(block):
    if block.get("type") != "paragraph":
        return None
    text = "".join(part.get("text", {}).get("content") or part.get("plain_text", "")
                   for part in block.get("paragraph", {}).get("rich_text", []))
    if not text.startswith(_PREFIX):
        return None
    try:
        value = json.loads(text[len(_PREFIX):])
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("schema") == 2 else None


def _content_rows(client, page_id):
    rows = []
    for block in client.list_children(page_id):
        if _marker(block) is not None:
            continue
        kind = block.get("type", "")
        body = dict(block.get(kind) or {})
        if block.get("has_children"):
            body["children"] = _content_rows(client, block["id"])
        rows.append({"type": kind, kind: body})
    return rows


def document_fingerprint(client, page_id):
    """Hash the complete visible body, including nested content and formatting."""
    return content_fingerprint(_content_rows(client, page_id))


def content_fingerprint(rows):
    encoded = json.dumps(_normalize_content(rows), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_content_rows(rows):
    """Keep visible content while excluding Notion response metadata.

    Stable prompts must not change with a refreshed file signature, rich-text
    convenience fields, block timestamps or API object identifiers. IDs inside
    mentions remain part of the visible content.
    """
    clean = []
    for row in rows:
        kind = row.get("type", "")
        body = dict(row.get(kind) or {})
        if isinstance(body.get("children"), list):
            body["children"] = canonical_content_rows(body["children"])
        clean.append({"type": kind, kind: body})
    return _normalize_content(clean)


def _normalize_content(rows):
    def normalize(value):
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            result = {key: normalize(item) for key, item in value.items()
                      if key not in {"plain_text", "href", "expiry_time"}}
            if isinstance(result.get("file"), dict) and isinstance(result["file"].get("url"), str):
                # Notion's internal file URL refreshes its access signature;
                # the referenced object path, rather than that token, is content.
                parts = urlsplit(result["file"]["url"])
                result["file"]["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            return result
        return value
    return normalize(rows)


def published_status(client, note_id, source_id):
    markers = [_marker(row) for row in client.list_children(note_id)]
    marker = next((value for value in reversed(markers) if value is not None), {})
    if not marker:
        return {"accepted": False}
    page = client.get_page(note_id)
    accepted = bool(marker.get("source_id") == source_id and marker.get("note_id") == note_id
                    and marker.get("accepted_at") and marker.get("lecture_id")
                    and page.get("parent", {}).get("page_id") == marker.get("lecture_id")
                    and not page.get("archived") and not page.get("in_trash")
                    and marker.get("remote_sha256") == document_fingerprint(client, note_id))
    return {**marker, "accepted": accepted}


def accept_published(job, *, client, notes_sha256, confirmed):
    if not confirmed:
        raise ValueError("请先审阅 Notion 总结并明确确认验收。")
    current = status(job)
    try:
        version = json.loads((job.directory / "published-note-version.json").read_text("utf-8"))
    except (OSError, ValueError):
        version = {}
    if (not current["notes_sha256"] or current["notes_sha256"] != notes_sha256
            or version.get("notes_sha256") != notes_sha256
            or version.get("source_id") != current["source_id"]
            or not version.get("note_id") or not version.get("lecture_id")):
        raise ValueError("总结尚未验证发布，或版本已变化；请重新发布并审阅。")
    note_id = version["note_id"]
    page = client.get_page(note_id)
    if (page.get("parent", {}).get("page_id") != version["lecture_id"]
            or page.get("archived") or page.get("in_trash")
            or document_fingerprint(client, note_id) != version.get("remote_sha256")):
        raise ValueError("Notion 总结位置或内容已变化，请重新发布并审阅。")
    marker = {**version, "schema": 2,
              "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    block = {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": _PREFIX + json.dumps(marker, ensure_ascii=False)}}]}}
    client.append_blocks(note_id, [block], retry=False)
    verified = published_status(client, note_id, current["source_id"])
    if not verified["accepted"] or verified.get("remote_sha256") != marker["remote_sha256"]:
        raise ValueError("Notion 验收写入后未能验证，请重新审阅。")
    accept(job, notes_sha256=notes_sha256, confirmed=True)
    path = job.directory / "summary-acceptance.json"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(marker, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)
    return {**verified, **status(job)}


def status(job) -> dict:
    path = job.directory / "notes.md"
    try:
        raw = path.read_bytes()
        source_id = recording_source_id(job.recording)
    except (OSError, ValueError):
        return {"accepted": False, "notes_sha256": "", "source_id": ""}
    digest = hashlib.sha256(raw).hexdigest() if raw.strip() else ""
    try:
        marker = json.loads((job.directory / "summary-acceptance.json").read_text("utf-8"))
    except (OSError, ValueError):
        marker = {}
    accepted = bool(digest and isinstance(marker, dict) and marker.get("schema") in {1, 2}
                    and marker.get("source_id") == source_id
                    and marker.get("notes_sha256") == digest and marker.get("accepted_at"))
    return {"accepted": accepted, "notes_sha256": digest, "source_id": source_id,
            "accepted_at": marker.get("accepted_at", "") if accepted else "",
            "schema": marker.get("schema"), "note_id": marker.get("note_id", ""),
            "remote_sha256": marker.get("remote_sha256", "")}


def accept(job, *, notes_sha256: str, confirmed: bool) -> dict:
    if not confirmed:
        raise ValueError("请先审阅总结，并确认已核对讲次内容与教学网要求。")
    current = status(job)
    if not current["notes_sha256"] or current["notes_sha256"] != notes_sha256:
        raise ValueError("总结内容已变化或为空，请重新打开并审阅。")
    marker = {"schema": 1, "source_id": current["source_id"],
              "notes_sha256": notes_sha256,
              "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path = job.directory / "summary-acceptance.json"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(marker, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)
    result = status(job)
    if not result["accepted"]:
        raise ValueError("总结在验收期间发生变化，请重新审阅。")
    return result
