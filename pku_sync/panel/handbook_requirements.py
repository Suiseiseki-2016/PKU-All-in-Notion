"""Surface coursework hidden in course handbooks without inventing assignments.

The PDF text is shown as source evidence. It is never converted into a formal
Blackboard task, a normalized deadline, or a submission target.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from pypdf.errors import PdfReadError

from .recordings_api import _safe_attachment_path


_HANDBOOK = re.compile(r"课程手册|学生手册|教学大纲|syllabus|course\s*handbook", re.I)
_COURSEWORK = re.compile(
    r"课程考核|平时成绩|期中成绩|期末成绩|提交内容|截止|上交|交至|"
    r"考试|出勤|小测|课堂展示|口头报告|文字报告|小组作业|课程作业"
)
_MAX_PDF_BYTES = 20_000_000
_MAX_PAGES = 60
_MAX_EXCERPT = 1450
_ENGLISH_DATE = (r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
                 r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
                 r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}"
                 r"(?:st|nd|rd|th)?\b")
_TIME_LINE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日|24\s*(?:点|:00)|" + _ENGLISH_DATE, re.I)
_DELIVERABLE = re.compile(r"提交|文字报告|幻灯片|科普视频|文献|邮件|文件|所选")
_PREVIOUS_DELIVERABLE = re.compile(r"提交|文字报告|幻灯片|科普视频|作业|文件")
_DATED_DELIVERY = re.compile(r"提交|上交|发至|发予|发送|递交")
_MAX_PREVIEW_ROWS = 8
_PRESENCE_TYPES = (
    ("attendance", "出勤", re.compile(r"出勤|考勤|签到"),
     "出勤计分或签到要求：笔记不能代替本人出勤；请核对具体方式。"),
    ("quiz", "小测", re.compile(r"小测|\bquiz\b", re.I),
     "小测需要本人作答；是否要求到场，请核对老师安排。"),
    ("presentation", "课堂展示", re.compile(r"课堂展示|口头报告|\bpresentation\b", re.I),
     "课堂展示或报告的个人参与安排，请向老师或助教核对。"),
    ("exam", "考试", re.compile(r"考试形式|考试时间|期末考试|闭卷考试|\bexam\b", re.I),
     "考试形式和参与地点以最新通知为准；笔记不能代替本人考试。"),
)


def _local_pdf(materials_dir: Path, item: dict, filename: str) -> Path | None:
    """Resolve a catalogued file only inside this course's material directory."""
    if Path(filename).name != filename or not filename.lower().endswith(".pdf"):
        return None
    relative = Path(str(item.get("path") or ""))
    if relative.is_absolute() or ".." in relative.parts:
        return None
    root = materials_dir.resolve()
    # Blackboard items often name a virtual folder; downloaded files live in
    # its parent (as with 教学大纲/课程手册及时间安排).
    candidates = [root / relative.parent / filename, root / relative / filename]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    return None


def cache_handbook_pdf(materials_dir: Path, item: dict, filename: str, payload: bytes) -> bool:
    """Cache only a catalogued, bounded PDF inside this course's materials."""
    if (Path(filename).name != filename or not filename.lower().endswith(".pdf")
            or not _HANDBOOK.search(filename + " " + str(item.get("title") or "") + " "
                                    + str(item.get("path") or ""))
            or not payload.startswith(b"%PDF-") or len(payload) > _MAX_PDF_BYTES):
        return False
    relative = Path(str(item.get("path") or ""))
    if relative.is_absolute() or ".." in relative.parts:
        return False
    root = materials_dir.resolve()
    target = (root / relative.parent / filename).resolve()
    if not target.is_relative_to(root):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + ".",
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        temporary.replace(target)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _pdf_pages(path: Path) -> list[tuple[int, str]]:
    from pypdf import PdfReader

    if path.stat().st_size > _MAX_PDF_BYTES:
        return []
    reader = PdfReader(str(path))
    return [(index + 1, page.extract_text() or "")
            for index, page in enumerate(reader.pages[:_MAX_PAGES])]


def _time_preview(pages: list[tuple[int, str]]) -> list[dict]:
    """Extract original lines with time/delivery cues, not interpreted due dates."""
    candidates = []
    seen = set()
    for page_number, text in pages:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if not _TIME_LINE.search(line):
                continue
            if (page_number, line) in seen:
                continue
            seen.add((page_number, line))
            previous = lines[index - 1] if index else ""
            context = (previous if _PREVIOUS_DELIVERABLE.search(previous)
                       and _DATED_DELIVERY.search(line) else "")
            excerpt = (context + "\n" if context else "") + line
            rank = (0 if re.search(r"24\s*(?:点|:00)", line) else
                    1 if re.search(r"考试时间|期末考试|Presentation|小测", line, re.I) else
                    2 if _DELIVERABLE.search(line) else 3)
            candidates.append((rank, page_number, index, excerpt))
    selected = sorted(candidates, key=lambda row: row[:3])[:_MAX_PREVIEW_ROWS]
    selected.sort(key=lambda row: (row[1], row[2]))
    return [{"page": page, "text": excerpt} for _, page, _, excerpt in selected]


def _presence_preview(pages: list[tuple[int, str]]) -> list[dict]:
    """Keep one original source line per participation type; infer no venue."""
    found = {}
    for page_number, text in pages:
        for line in (line.strip() for line in text.splitlines()):
            if not line:
                continue
            for kind, label, pattern, note in _PRESENCE_TYPES:
                if not pattern.search(line):
                    continue
                # "考试时上交" describes a hand-in; prefer the explicit exam
                # format/time row when available on the same or later page.
                if kind == "exam" and not re.search(r"考试形式|考试时间|期末考试$|闭卷考试|\bexam\b", line, re.I):
                    continue
                if kind == "presentation" and line == "课堂展示：":
                    continue
                rank = (0 if (kind == "attendance" and "课堂出勤" in line) or
                            (kind == "presentation" and "课堂展示：" in line) or
                            (kind == "exam" and "考试形式" in line) else 1)
                if kind in found and found[kind][0] <= rank:
                    continue
                found[kind] = (rank, {"kind": kind, "label": label, "page": page_number,
                                      "text": line, "note": note})
    return [found[kind][1] for kind, *_ in _PRESENCE_TYPES if kind in found]


def handbook_review_cards(materials_dir: Path, materials: list[dict]) -> list[dict]:
    """Return source-page review cards for indexed PDF handbooks.

    A page needs more than an isolated keyword (a table of contents often has
    those). Missing, scanned, or unparsable PDFs still produce a review card.
    """
    cards = []
    for material_index, item in enumerate(materials):
        if not isinstance(item, dict):
            continue
        files = item.get("files") or []
        if not isinstance(files, list):
            continue
        for file_index, name in enumerate(files):
            if not isinstance(name, str) or not name.lower().endswith(".pdf"):
                continue
            if not _HANDBOOK.search(name + " " + str(item.get("title") or "") + " " +
                                    str(item.get("path") or "")):
                continue
            local = _local_pdf(materials_dir, item, name)
            pages = []
            preview = []
            presence = []
            if local:
                try:
                    pdf_pages = _pdf_pages(local)
                    for number, raw in pdf_pages:
                        text = "\n".join(line.strip() for line in raw.splitlines() if line.strip())
                        if text.lstrip().startswith("目录\n") and text.count("....") >= 3:
                            continue
                        signals = set(_COURSEWORK.findall(text))
                        if len(text) >= 250 and (len(signals) >= 2 or
                                                 (_TIME_LINE.search(text) and _DATED_DELIVERY.search(text))):
                            pages.append({"page": number, "excerpt": text[:_MAX_EXCERPT],
                                          "truncated": len(text) > _MAX_EXCERPT})
                    selected_pages = [(number, raw) for number, raw in pdf_pages
                                      if any(page["page"] == number for page in pages)]
                    preview = _time_preview(selected_pages)
                    presence = _presence_preview(selected_pages)
                except (OSError, ValueError, KeyError, PdfReadError):
                    pass
            if not pages and local:
                # A readable PDF can still hold requirements in a scan or a
                # format outside these conservative heuristics.
                status = "未检出可自动核对的考核页；请人工检查整份手册。"
            elif pages:
                status = "可能包含未进入正式作业索引的要求；请对照手册原文和老师最新通知核对。"
            else:
                status = "尚未取得可读取的手册正文；请打开原文件核对考核和交付要求。"
            cards.append({
                "title": f"课程手册待核对 · {name}",
                "source": "handbook_review",
                "filename": name,
                "material_index": material_index,
                "file_index": file_index,
                "material_path": str(item.get("path") or ""),
                "parent_content_id": str(item.get("parent_content_id") or ""),
                "attachments": [{"filename": name, "path": safe_path}
                                for asset in item.get("attachments", [])
                                if isinstance(asset, dict) and asset.get("filename") == name
                                if (safe_path := _safe_attachment_path(asset.get("path")))],
                "pages": pages,
                "time_preview": preview,
                "presence_preview": presence,
                "status": status,
            })
    return cards
