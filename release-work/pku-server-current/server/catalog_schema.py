"""Small, credential-free official metadata contract shared with the relay."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from urllib.parse import urlsplit

SECTIONS = ("recordings", "materials", "assignments", "announcements")
ASSESSMENT_KINDS = frozenset({"考试", "测试", "测验", "小测", "Test", "Quiz", "Exam", "Survey"})
FIELDS = {
    "recordings": {"source_id", "title", "recorded_at", "teacher", "duration_seconds"},
    "materials": {"source_id", "content_id", "title", "kind", "path", "parent_content_id",
                  "body_text", "files", "attachments", "source_links"},
    "assignments": {"source_id", "content_id", "title", "due_at", "source", "instructions",
                    "files", "source_links"},
    "announcements": {"source_id", "id", "title", "posted_at", "author", "body_text", "files"},
}


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_snapshot(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema", "authority", "course_id", "course", "sections"}:
        raise ValueError("invalid snapshot envelope")
    if type(value["schema"]) is not int or value["schema"] != 1 or value["authority"] != "course.pku.edu.cn":
        raise ValueError("invalid official authority")
    if not isinstance(value["course_id"], str) or not value["course_id"] or len(value["course_id"]) > 100:
        raise ValueError("invalid course identity")
    course = value["course"]
    if not isinstance(course, dict) or set(course) != {"course_id", "name", "code", "term"}:
        raise ValueError("invalid course metadata")
    if not all(isinstance(v, str) for v in course.values()) or course["course_id"] != value["course_id"] or not course["name"]:
        raise ValueError("invalid course metadata")
    sections = value["sections"]
    if not isinstance(sections, dict) or set(sections) != set(SECTIONS):
        raise ValueError("missing official section")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 512 * 1024:
        raise ValueError("snapshot exceeds metadata limit")
    for name in SECTIONS:
        section = sections[name]
        if (not isinstance(section, dict) or set(section) != {"complete", "items"}
                or section["complete"] is not True or not isinstance(section["items"], list)):
            raise ValueError("incomplete official section")
        identities = set()
        for row in section["items"]:
            if not isinstance(row, dict) or set(row) - FIELDS[name]:
                raise ValueError("unsupported metadata field")
            identity = row.get("source_id")
            if not isinstance(identity, str) or not identity or identity in identities:
                raise ValueError("missing or duplicate source ID")
            identities.add(identity)
            if not isinstance(row.get("title"), str) or not row["title"]:
                raise ValueError("missing source title")
            if name == "recordings":
                try:
                    datetime.fromisoformat(row["recorded_at"])
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError("missing or invalid official lecture timestamp") from exc
            for key, item in row.items():
                if key == "duration_seconds":
                    if type(item) is not int or item < 0:
                        raise ValueError("invalid recording duration")
                elif key == "files":
                    if not isinstance(item, list) or any(not isinstance(f, str) for f in item):
                        raise ValueError("invalid filenames")
                elif key in {"attachments", "source_links"}:
                    if not isinstance(item, list):
                        raise ValueError("invalid source links")
                    for link in item:
                        fields = {"filename", "path"} if key == "attachments" else {"label", "url"}
                        if not isinstance(link, dict) or set(link) != fields or any(not isinstance(v, str) for v in link.values()):
                            raise ValueError("invalid source link")
                        url = link.get("path", link.get("url", ""))
                        parsed = urlsplit(url)
                        if (parsed.query or parsed.fragment or parsed.username or parsed.password
                                or (parsed.netloc and parsed.hostname != "course.pku.edu.cn")
                                or parsed.scheme not in {"", "https"} or url.startswith("//")):
                            raise ValueError("private or non-campus source URL")
                elif not isinstance(item, str):
                    raise ValueError("invalid metadata value")
    return json.loads(json.dumps(value, ensure_ascii=False))
