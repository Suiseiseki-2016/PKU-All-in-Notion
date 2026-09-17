"""Filesystem layout and attachment downloads.

    data/<course-slug>/
      course.json
      announcements/<date>_<title>.md
      materials/<area>/<folder>/_index.md      one per Blackboard folder
      materials/<area>/<folder>/<file.pdf>
      assignments.md
      deadlines.json
      recordings/index.json
      recordings/<date>_<title>/...
      manifest.json

The tree deliberately mirrors what a student sees in Blackboard, so a file is
findable without consulting the manifest.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

import httpx

from .manifest import Manifest

# Reserved on Windows, and the mirror has to work there too.
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
MAX_COMPONENT = 120


def safe_name(value: str, fallback: str = "untitled") -> str:
    """Turn arbitrary Blackboard text into one portable path component."""
    text = unicodedata.normalize("NFC", value or "")
    text = _ILLEGAL_RE.sub("_", text)
    text = " ".join(text.split()).strip(" .")
    if text.split(".")[0].upper() in _WINDOWS_RESERVED:
        text = f"_{text}"
    if len(text) > MAX_COMPONENT:
        stem, dot, suffix = text.rpartition(".")
        if dot and len(suffix) <= 8:
            text = stem[: MAX_COMPONENT - len(suffix) - 1] + "." + suffix
        else:
            text = text[:MAX_COMPONENT]
    return text or fallback


def safe_path(*parts: str) -> Path:
    return Path(*[safe_name(part) for part in parts if part])


def download_attachment(
    client: httpx.Client,
    url: str,
    directory: Path,
    fallback_name: str,
    manifest: Manifest,
    root: Path,
) -> tuple[Path | None, bool]:
    """Fetch one attachment unless the manifest shows the local copy is current.

    Returns the written path and whether a download actually happened. The
    filename Blackboard sends in Content-Disposition wins over the link text,
    which is often the item's title and carries no extension.
    """
    identity = _identity(url)
    provisional = directory / safe_name(fallback_name, "attachment")
    if manifest.is_current(_rel(provisional, root), identity, root):
        return provisional, False

    try:
        resp = client.get(
            url, follow_redirects=True, headers={"Referer": "https://course.pku.edu.cn/"}
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return None, False

    name = _disposition_name(resp) or fallback_name
    if not Path(name).suffix:
        name = f"{name}{_suffix_from_type(resp.headers.get('content-type', ''))}"

    target = directory / safe_name(name, "attachment")
    rel = _rel(target, root)
    if manifest.is_current(rel, identity, root):
        return target, False

    directory.mkdir(parents=True, exist_ok=True)
    target.write_bytes(resp.content)
    manifest.record(rel, identity, resp.content)
    if target != provisional:
        # The provisional name was only a guess; do not leave it in the manifest.
        manifest.forget(_rel(provisional, root))
    return target, True


def _identity(url: str) -> str:
    """Strip the signed query so a re-signed link is not mistaken for a new file.

    Blackboard mints fresh download tokens on every page render, but the path
    still carries the content id, which changes when the file is replaced.
    """
    return url.split("?", 1)[0]


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _disposition_name(resp: httpx.Response) -> str:
    disposition = resp.headers.get("content-disposition", "")
    if not disposition:
        return ""
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    if match:
        return unquote(match.group(1)).strip('"')
    match = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    return unquote(match.group(1)).strip() if match else ""


def _suffix_from_type(content_type: str) -> str:
    base = content_type.split(";")[0].strip().lower()
    return {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/zip": ".zip",
        "text/plain": ".txt",
        "text/html": ".html",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
    }.get(base, "")


def write_text(path: Path, content: str) -> bool:
    """Write only when the content differs, so mtimes track real changes."""
    if path.exists() and path.read_text("utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8")
    return True
