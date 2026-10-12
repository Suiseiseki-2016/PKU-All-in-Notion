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
from xml.etree import ElementTree

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
            suffix = Path(filename).suffix.lower()
            if not filename or (suffix and suffix not in SUPPORTED):
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


def _document_suffix(path: Path) -> str:
    """Infer only our supported formats from bytes, never response filenames."""
    with path.open("rb") as handle:
        magic = handle.read(5)
    if magic == b"%PDF-":
        return ".pdf"
    if magic[:4] != b"PK\x03\x04":
        return ""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) > 5000 or len(names) != len(set(names)) or any(
                    name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/")
                    for name in names):
                return ""
            candidates = [(suffix, core) for suffix, core in
                          ((".pptx", "ppt/presentation.xml"), (".docx", "word/document.xml"))
                          if core in names]
            if len(candidates) != 1:
                return ""
            suffix, core = candidates[0]
            if not _looks_like_document(path, suffix) or "[Content_Types].xml" not in names:
                return ""
            if archive.getinfo("[Content_Types].xml").file_size > 1_000_000:
                return ""
            content_types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
            prefix = "http://schemas.openxmlformats.org/package/2006/content-types"
            expected_type = ("application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
                             if suffix == ".pptx" else
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml")
            if content_types.tag != "{" + prefix + "}Types" or not any(
                    node.tag == "{" + prefix + "}Override" and node.get("PartName") == "/" + core
                    and node.get("ContentType") == expected_type for node in content_types):
                return ""
            document = ElementTree.fromstring(archive.read(core))
            allowed_roots = ({"{http://schemas.openxmlformats.org/presentationml/2006/main}presentation",
                              "{http://purl.oclc.org/ooxml/presentationml/main}presentation"}
                             if suffix == ".pptx" else
                             {"{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document",
                              "{http://purl.oclc.org/ooxml/wordprocessingml/main}document"})
            return suffix if document.tag in allowed_roots else ""
    except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError, KeyError):
        return ""


def _fetch_document(client, url: str, destination: Path, budget: int) -> Path | None:
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not _safe_webdav_url(url):
            return None
        with client.stream("GET", url, follow_redirects=True,
                           headers={"Referer": "https://course.pku.edu.cn/"}) as response:
            response.raise_for_status()
            if not _safe_webdav_url(str(response.url)):
                return None
            length = response.headers.get("content-length")
            if length and int(length) > min(MAX_FILE_BYTES, budget):
                return None
            count = 0
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    count += len(chunk)
                    if count > min(MAX_FILE_BYTES, budget):
                        return None
                    handle.write(chunk)
        suffix = destination.suffix.lower()
        if not suffix:
            suffix = _document_suffix(temporary)
            if not suffix:
                return None
            typed = destination.with_name(destination.name + suffix)
            # Never overwrite an unverified or edited existing inferred source.
            if typed.exists():
                if typed.is_symlink() or not typed.is_file() or typed.stat().st_size > min(MAX_FILE_BYTES, budget):
                    return None
                if hashlib.sha256(typed.read_bytes()).digest() != hashlib.sha256(temporary.read_bytes()).digest():
                    return None
                return typed
        else:
            typed = destination
        if suffix not in SUPPORTED or not _looks_like_document(temporary, suffix):
            return None
        temporary.replace(typed)
        return typed
    except Exception as exc:
        logger.warning("lecture material download failed: %s", type(exc).__name__)
        return None
    finally:
        temporary.unlink(missing_ok=True)


def _fetch_bounded(client, url: str, destination: Path, budget: int) -> bool:
    # Preserve the existing fixed-extension caller contract.
    return _fetch_document(client, url, destination, budget) is not None


def _source_binding(job, row, asset) -> str:
    value = {"course": job.recording.course_id, "date": job.recording.date,
             "content_id": row.get("content_id"), "title": row.get("title"),
             "path": row.get("path"), "filename": asset.get("filename"),
             "official_asset_path": asset.get("path")}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _cached_inferred(base: Path, binding: str, budget: int) -> Path | None:
    receipt = _read_json(base.with_name(base.name + ".source.json"), {})
    if (not isinstance(receipt, dict) or set(receipt) != {"schema", "source_sha256", "suffix", "bytes", "sha256"}
            or receipt.get("schema") != 1 or receipt.get("source_sha256") != binding
            or receipt.get("suffix") not in SUPPORTED):
        return None
    typed = base.with_name(base.name + receipt["suffix"])
    try:
        if (not typed.is_file() or typed.is_symlink() or type(receipt.get("bytes")) is not int
                or typed.stat().st_size != receipt["bytes"] or receipt["bytes"] > min(MAX_FILE_BYTES, budget)
                or hashlib.sha256(typed.read_bytes()).hexdigest() != receipt.get("sha256")
                or _document_suffix(typed) != receipt["suffix"]):
            return None
    except OSError:
        return None
    return typed


def _save_inferred_receipt(base: Path, typed: Path, binding: str):
    receipt = {"schema": 1, "source_sha256": binding, "suffix": typed.suffix.lower(),
               "bytes": typed.stat().st_size, "sha256": hashlib.sha256(typed.read_bytes()).hexdigest()}
    target = base.with_name(base.name + ".source.json")
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True), "utf-8")
    temporary.replace(target)


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
            inferred = not Path(filename).suffix
            binding = _source_binding(job, row, asset)
            cached = _cached_inferred(local, binding, remaining) if inferred else local
            if cached is not None and cached.is_file() and cached.stat().st_size <= min(MAX_FILE_BYTES, remaining) and _looks_like_document(cached):
                paths.append(cached)
                remaining -= cached.stat().st_size
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
            expected_url = _safe_webdav_url(identity)
            same_official_path = url and unquote(urlsplit(url).path) == unquote(urlsplit(expected_url).path)
            downloaded = _fetch_document(client, url, local, remaining) if same_official_path else None
            if downloaded is None:
                failures.append({"title": filename, "locator": "", "path": "", "text": "", "status": "unavailable"})
                processed += 1
                continue
            if inferred:
                _save_inferred_receipt(local, downloaded, binding)
            paths.append(downloaded)
            remaining -= downloaded.stat().st_size
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
