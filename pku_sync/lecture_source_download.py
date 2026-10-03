"""Bounded, on-demand Teaching Network attachments for one lecture note.

The catalog refresh only saves metadata.  Downloading happens when a student
actively organizes a lecture, never while browsing or refreshing all courses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .pipeline import RecordingJob
from .store import safe_name

logger = logging.getLogger(__name__)

SUPPORTED = frozenset({".pdf", ".pptx", ".docx"})
MAX_FILES = 6
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
_ASSIGNMENT_WORDS = ("作业", "考试", "测试", "测验", "提交")


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return fallback


def _explicit_date(value: str, day: date) -> bool:
    normalized = unicodedata.normalize("NFKC", value or "")
    # Keep the boundaries between year, month and day.  Removing every
    # non-digit could accidentally combine a week number and a content ID.
    pattern = (rf"(?<!\d){day.year}(?:[-./_年]\s*)?0?{day.month}"
               rf"(?:[-./_月]\s*)?0?{day.day}(?:日)?(?!\d)")
    return re.search(pattern, normalized) is not None


def _single_lecture_that_day(job: RecordingJob) -> bool:
    rows = _read_json(job.course_dir / "recordings" / "index.json", [])
    return isinstance(rows, list) and sum(
        isinstance(row, dict) and str(row.get("recorded_at") or "").startswith(job.recording.date)
        for row in rows
    ) == 1


def _candidate_attachments(job: RecordingJob, recording_key: str) -> list[tuple[dict, dict]]:
    material_dir = job.course_dir / "materials"
    rows = _read_json(material_dir / "index.json", [])
    matches = _read_json(material_dir / "matches.json", {})
    if not isinstance(rows, list):
        return []
    if not isinstance(matches, dict):
        matches = {}
    try:
        day = date.fromisoformat(job.recording.date)
    except ValueError:
        return []
    unique_day = _single_lecture_that_day(job)
    chosen = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        path = str(row.get("path") or "")
        kind = str(row.get("kind") or "")
        if kind in {"作业", "考试", "测试"} or any(word in title for word in _ASSIGNMENT_WORDS):
            continue
        override = matches.get(path)
        manual = override == recording_key
        if override is not None and not manual:
            continue
        assets = row.get("attachments") or []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            filename = str(asset.get("filename") or "")
            if Path(filename).suffix.lower() not in SUPPORTED:
                continue
            dated = unique_day and any(
                _explicit_date(value, day) for value in (title, path, filename)
            )
            if manual or dated:
                chosen.append((row, asset))
    return chosen


def _safe_webdav_url(url: str) -> str:
    """Accept only the course host's WebDAV attachment paths."""
    if not isinstance(url, str) or "\\" in url:
        return ""
    try:
        parts = urlsplit(url)
        if parts.scheme or parts.netloc:
            if (parts.scheme != "https" or parts.hostname != "course.pku.edu.cn"
                    or parts.port not in (None, 443) or parts.username or parts.password):
                return ""
    except ValueError:
        return ""
    decoded_path = unquote(parts.path)
    if (not decoded_path.startswith("/bbcswebdav/") or "\\" in decoded_path
            or ".." in decoded_path.split("/")):
        return ""
    if parts.fragment:
        return ""
    return url if parts.scheme else "https://course.pku.edu.cn" + url


def _looks_like_document(path: Path, suffix: str | None = None) -> bool:
    suffix = (suffix or path.suffix).lower()
    with path.open("rb") as handle:
        magic = handle.read(5)
    if suffix == ".pdf":
        return magic == b"%PDF-"
    if magic[:4] != b"PK\x03\x04":
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            if sum(member.file_size for member in archive.infolist()) > 100 * 1024 * 1024:
                return False
            required = "word/document.xml" if suffix == ".docx" else "ppt/presentation.xml"
            return required in archive.namelist() and archive.getinfo(required).file_size <= 20 * 1024 * 1024
    except (OSError, ValueError, zipfile.BadZipFile):
        return False


def _fetch_bounded(client, url: str, destination: Path, budget: int) -> bool:
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        with client.stream("GET", url, follow_redirects=True,
                           headers={"Referer": "https://course.pku.edu.cn/"}) as response:
            response.raise_for_status()
            if not _safe_webdav_url(str(response.url)):
                return False
            length = response.headers.get("content-length")
            if length and int(length) > min(MAX_FILE_BYTES, budget):
                return False
            count = 0
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    count += len(chunk)
                    if count > min(MAX_FILE_BYTES, budget):
                        return False
                    handle.write(chunk)
        if not _looks_like_document(temporary, destination.suffix):
            # Validation needs the real extension, but never expose a partial
            # file under its final path.
            return False
        temporary.replace(destination)
        return True
    except Exception as exc:
        logger.warning("lecture material download failed: %s", type(exc).__name__)
        return False
    finally:
        temporary.unlink(missing_ok=True)


def prepare_lesson_sources(job: RecordingJob, recording_key: str, settings, stage=None) -> tuple[list[Path] | None, list[dict]]:
    """Return verified local files plus unavailable source rows for this lecture."""
    candidates = _candidate_attachments(job, recording_key)
    if not candidates:
        # Older full-sync directories may already hold dated files.  The
        # source parser's strict date scan is safe only with one lecture that
        # day; an empty list disables fallback for ambiguous same-day pairs.
        return (None if _single_lecture_that_day(job) else []), []
    paths: list[Path] = []
    failures: list[dict] = []
    selected = candidates[:MAX_FILES]
    # The panel passes a 24-character hash, but this helper also has tests and
    # non-panel callers.  Never use a caller-supplied identifier as a path.
    cache_key = hashlib.sha256(recording_key.encode("utf-8")).hexdigest()[:24]
    for row, asset in candidates[MAX_FILES:]:
        failures.append({"title": str(asset.get("filename") or row.get("title") or "课件"),
                         "locator": "", "path": "", "text": "", "status": "unavailable"})
    live = None
    client = None
    remaining = MAX_TOTAL_BYTES
    processed = 0
    try:
        for index, (row, asset) in enumerate(selected):
            filename = str(asset.get("filename") or "")
            identity = str(asset.get("path") or "")
            if not _safe_webdav_url(identity):
                failures.append({"title": filename, "locator": "", "path": "", "text": "", "status": "unavailable"})
                processed += 1
                continue
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
            local = (job.course_dir / "materials" / ".lecture-sources" / cache_key /
                     f"{index:02d}-{digest}-{safe_name(filename)}")
            if local.is_file() and local.stat().st_size <= min(MAX_FILE_BYTES, remaining) and _looks_like_document(local):
                paths.append(local)
                remaining -= local.stat().st_size
                processed += 1
                continue
            if stage:
                stage(f"读取本讲课件：{index + 1}/{len(selected)}")
            if not getattr(settings, "pku_username", "") or not getattr(settings, "pku_password", ""):
                failures.append({"title": filename, "locator": "", "path": "", "text": "", "status": "unavailable"})
                processed += 1
                continue
            if client is None:
                from .auth import get_session
                from .materials import walk_materials
                client = get_session(username=settings.pku_username, password=settings.pku_password)
                live, _ = walk_materials(client, job.recording.course_id)
            matching = next((item for item in live if item.path == row.get("path")
                             and item.title == row.get("title")
                             and item.content_id == row.get("content_id")), None)
            fresh = next((item for item in matching.attachments if item.filename == filename), None) if matching else None
            url = _safe_webdav_url(fresh.url) if fresh else ""
            if not url or not _fetch_bounded(client, url, local, remaining):
                failures.append({"title": filename, "locator": "", "path": "", "text": "", "status": "unavailable"})
                processed += 1
                continue
            paths.append(local)
            remaining -= local.stat().st_size
            processed += 1
    except Exception as exc:
        logger.warning("lecture material lookup failed: %s", type(exc).__name__)
        for row, asset in selected[processed:]:
            failures.append({"title": str(asset.get("filename") or row.get("title") or "课件"),
                             "locator": "", "path": "", "text": "", "status": "unavailable"})
    finally:
        if client is not None:
            client.close()
    return paths, failures
