"""Read two real course handbooks into a local probe, without touching app data."""

from __future__ import annotations

import json
from pathlib import Path

from pku_sync.auth import get_session
from pku_sync.config import Settings
from pku_sync.lecture_source_download import MAX_FILE_BYTES, _fetch_bounded, _safe_webdav_url
from pku_sync.materials import walk_materials
from pku_sync.panel.campus_catalog import _courses, campus_course


def main() -> None:
    installed = Path.home() / "PKU-All-in-Notion" / "data"
    found = next(((folder, key, metadata) for folder, key, title, metadata in _courses(installed)
                  if "发展心理学" in title), None)
    if found is None:
        raise RuntimeError("development psychology course not found")
    source_folder, course_id, metadata = found
    materials = json.loads((source_folder / "materials" / "index.json").read_text("utf-8"))
    row = next((item for item in materials if item.get("title") == "课程手册及时间安排"), None)
    if row is None or len(row.get("files") or []) != 2:
        raise RuntimeError("expected two indexed handbook PDFs")
    settings = Settings(_env_file=Path.home() / "PKU-All-in-Notion" / ".env")
    if not settings.pku_username or not settings.pku_password:
        raise RuntimeError("Teaching Network credentials unavailable")
    probe = Path(".probe/handbook-live")
    course = probe / "发展心理学"
    target_dir = course / "materials" / Path(row["path"]).parent
    target_dir.mkdir(parents=True, exist_ok=True)
    (course / "course.json").write_text(json.dumps(metadata, ensure_ascii=False), "utf-8")
    (course / "materials" / "index.json").write_text(json.dumps([row], ensure_ascii=False), "utf-8")
    downloaded = []
    client = get_session(username=settings.pku_username, password=settings.pku_password)
    try:
        live, _ = walk_materials(client, course_id)
        matching = next((item for item in live if item.path == row.get("path")
                         and item.title == row.get("title")
                         and item.content_id == row.get("content_id")), None)
        if matching is None:
            raise RuntimeError("fresh course handbook listing did not match indexed item")
        for filename in row["files"]:
            fresh = next((asset for asset in matching.attachments
                          if asset.filename == filename), None)
            url = _safe_webdav_url(fresh.url) if fresh else ""
            if not url:
                raise RuntimeError("handbook attachment has no safe fresh URL")
            target = target_dir / filename
            if not target.is_file() and not _fetch_bounded(client, url, target, MAX_FILE_BYTES):
                raise RuntimeError("handbook PDF download failed")
            downloaded.append({"filename": filename, "bytes": target.stat().st_size})
    finally:
        client.close()
    cards = campus_course(probe, course_id)["handbook_reviews"]
    print(json.dumps({
        "downloaded": downloaded,
        "cards": [{"filename": card["filename"], "pages": [p["page"] for p in card["pages"]],
                   "time_preview": [p["text"][:100] for p in card["time_preview"]],
                   "presence": [p["label"] for p in card["presence_preview"]]}
                  for card in cards],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
