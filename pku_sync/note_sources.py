"""Conservatively match lecture materials to one recording.

Only an explicit full date is accepted.  An unrelated
document in the same course is worse than no document when notes are used as
the student's primary account of a lecture.
"""

from __future__ import annotations

import re
import unicodedata
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

from pypdf.errors import PdfReadError

_AUTO_SUPPORTED = {".pdf", ".pptx", ".docx"}
_SUPPORTED = _AUTO_SUPPORTED | {".md", ".txt"}
# Lecture uploads are eligible only through that lecture's explicit paths.
_EXCLUDED_DIRS = {"课程资料", "教学大纲", "课程作业", "参考资料", "参考文献", ".lecture-uploads"}
# The matching chapter may be near the end of a full-semester slide deck.
# The editor separately batches extracted text to fit requests. Any local
# page ceiling must be reported; individual page text must never be cropped.
_PAGE_LIMIT = 256


def _material_matches(stem: str, recording_date: date) -> bool:
    normalized = unicodedata.normalize("NFKC", stem)
    compact = re.sub(r"[^0-9]", "", normalized)
    return recording_date.strftime("%Y%m%d") in compact


def _clean(text: str) -> str:
    return text.strip()


def _text_pages(path: Path) -> list[tuple[str, str, str]]:
    text = path.read_text("utf-8").strip()
    chunk_size = 4000
    total = max(1, (len(text) + chunk_size - 1) // chunk_size)
    pages = []
    for index in range(min(total, _PAGE_LIMIT)):
        chunk = text[index * chunk_size:(index + 1) * chunk_size]
        label = "正文" if total == 1 else f"第 {index + 1} 节"
        pages.append((label, chunk, "readable" if chunk.strip() else "unreadable"))
    if total > _PAGE_LIMIT:
        pages.append((f"第 {_PAGE_LIMIT + 1}–{total} 节（共 {total} 节）", "", "truncated"))
    return pages


def _office_pages(path: Path) -> list[tuple[str, str, str]]:
    pages = []
    with zipfile.ZipFile(path) as archive:
        if path.suffix.lower() == ".pptx":
            names = sorted(
                (name for name in archive.namelist()
                 if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                key=lambda name: int(re.search(r"slide(\d+)", name).group(1)),
            )
            label = "第 {number} 张"
        else:
            names = ["word/document.xml"] if "word/document.xml" in archive.namelist() else []
            label = "正文"
        for index, name in enumerate(names[:_PAGE_LIMIT], 1):
            root = ElementTree.fromstring(archive.read(name))
            text = " ".join(node.text or "" for node in root.iter()
                            if node.tag.rsplit("}", 1)[-1] == "t")
            cleaned = _clean(text)
            pages.append((label.format(number=index), cleaned, "readable" if cleaned else "unreadable"))
        if len(names) > _PAGE_LIMIT:
            pages.append((f"第 {_PAGE_LIMIT + 1}–{len(names)} 张（共 {len(names)} 张）", "", "truncated"))
    return pages


def _pdf_pages(path: Path) -> list[tuple[str, str, str]]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    total_pages = len(reader.pages)
    result = []
    for index, page in enumerate(reader.pages[:_PAGE_LIMIT], 1):
        cleaned = _clean(page.extract_text() or "")
        # Extraction observes image presence, not whether a certain word
        # count sufficiently represents its meaning. Images are not OCR'd.
        has_images = bool(page.images)
        if not cleaned:
            status = "unreadable"
        elif has_images:
            status = "partial"
        else:
            status = "readable"
        result.append((f"第 {index} 页", cleaned, status))
    if total_pages > _PAGE_LIMIT:
        omitted = (f"第 {_PAGE_LIMIT + 1} 页" if total_pages == _PAGE_LIMIT + 1
                   else f"第 {_PAGE_LIMIT + 1}–{total_pages} 页")
        result.append((f"{omitted}（共 {total_pages} 页）", "", "truncated"))
    return result


def collect_note_sources(course_directory: Path, recording_date: str, title: str,
                         allowed_paths: list[Path] | None = None) -> list[dict]:
    """Return text-bearing pages from materials explicitly matched to a lecture.

    The title is kept in the interface for future exact title matches; fuzzy
    similarity is intentionally not used to select a potentially wrong week.
    """
    del title
    try:
        day = date.fromisoformat(recording_date[:10])
    except ValueError:
        return []
    course_directory = Path(course_directory).resolve()
    root = course_directory / "materials"
    if not root.is_dir():
        return []
    sources = []
    candidates = sorted(root.rglob("*")) if allowed_paths is None else allowed_paths
    supported = _AUTO_SUPPORTED if allowed_paths is None else _SUPPORTED
    seen: set[Path] = set()
    for candidate in candidates:
        path = Path(candidate).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if path in seen or not path.is_file() or path.suffix.lower() not in supported:
            continue
        seen.add(path)
        if allowed_paths is None and (any(part in _EXCLUDED_DIRS for part in relative.parts)
                                      or not _material_matches(path.stem, day)):
            continue
        try:
            if path.suffix.lower() in {".md", ".txt"}:
                pages = _text_pages(path)
            else:
                pages = _pdf_pages(path) if path.suffix.lower() == ".pdf" else _office_pages(path)
        except (OSError, ValueError, KeyError, EOFError, TypeError, PdfReadError,
                zipfile.BadZipFile, ElementTree.ParseError):
            pages = []
        if not pages:
            sources.append({"title": path.name, "locator": "", "path": str(path.relative_to(course_directory)),
                            "text": "", "status": "unreadable"})
        for page in pages:
            locator, content, status = (*page, "readable") if len(page) == 2 else page
            sources.append({"title": path.name, "locator": locator,
                            "path": str(path.relative_to(course_directory)), "text": content,
                            "status": status})
    return sources
