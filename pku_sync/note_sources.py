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

_SUPPORTED = {".pdf", ".pptx", ".docx"}
_EXCLUDED_DIRS = {"课程资料", "教学大纲", "课程作业", "参考资料", "参考文献"}
# The matching chapter may be near the end of a full-semester slide deck.
# AI requests still select at most eight pages / 12k characters per chapter;
# this is only a local extraction ceiling, not an upload allowance.
_PAGE_LIMIT = 256
_TEXT_LIMIT = 2500


def _material_matches(stem: str, recording_date: date) -> bool:
    normalized = unicodedata.normalize("NFKC", stem)
    compact = re.sub(r"[^0-9]", "", normalized)
    return recording_date.strftime("%Y%m%d") in compact


def _clean(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:_TEXT_LIMIT]


def _office_pages(path: Path) -> list[tuple[str, str]]:
    pages = []
    with zipfile.ZipFile(path) as archive:
        if path.suffix.lower() == ".pptx":
            names = sorted(
                (name for name in archive.namelist()
                 if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                key=lambda name: int(re.search(r"slide(\d+)", name).group(1)),
            )[:_PAGE_LIMIT]
            label = "第 {number} 张"
        else:
            names = ["word/document.xml"] if "word/document.xml" in archive.namelist() else []
            label = "正文"
        for index, name in enumerate(names, 1):
            root = ElementTree.fromstring(archive.read(name))
            text = " ".join(node.text or "" for node in root.iter()
                            if node.tag.rsplit("}", 1)[-1] == "t")
            cleaned = _clean(text)
            if cleaned:
                pages.append((label.format(number=index), cleaned))
    return pages


def _pdf_pages(path: Path) -> list[tuple[str, str, str]]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    total_pages = len(reader.pages)
    result = []
    for index, page in enumerate(reader.pages[:_PAGE_LIMIT], 1):
        cleaned = _clean(page.extract_text() or "")
        # A short text layer beside embedded images is often only the page
        # title or question number. Treating it as the complete slide/question
        # would give a student false confidence that the note covers it.
        has_images = bool(page.images)
        if not cleaned:
            status = "unreadable"
        elif has_images and len(cleaned) < 120:
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
    seen: set[Path] = set()
    for candidate in candidates:
        path = Path(candidate).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if path in seen or not path.is_file() or path.suffix.lower() not in _SUPPORTED:
            continue
        seen.add(path)
        if allowed_paths is None and (any(part in _EXCLUDED_DIRS for part in relative.parts)
                                      or not _material_matches(path.stem, day)):
            continue
        try:
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
