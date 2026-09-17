"""Lecture recordings published through Blackboard's PKU streaming-media module.

The path from a course to a downloadable file crosses three systems:

  1. course.pku.edu.cn  videoList.action?course_id=...
        A table of sessions; each row links to playVideo.action?token=...
  2. course.pku.edu.cn  playVideo.action?token=...
        Returns a page whose player iframe points at yjapise.pku.edu.cn's CAS
        endpoint. That CAS link is single-use, so it must be fetched right after
        the page that produced it.
  3. yjapise.pku.edu.cn CAS -> onlineroomse.pku.edu.cn/player?course_id&sub_id
        Following the redirect installs the streaming platform's session, after
        which get-course-subject-info returns the session's metadata, usually
        including a direct range-capable mp4 URL.

Only the last step needs the streaming platform's own numeric ids, which is why
they are discovered rather than configured.
"""

from __future__ import annotations

import json
import re

import httpx

from .models import Recording

STREAM_BASE = "/webapps/bb-streammedia-hqy-BBLEARN"
VIDEO_LIST = f"{STREAM_BASE}/videoList.action"
SUBJECT_INFO = (
    "https://yjapise.pku.edu.cn/vlabpassportapi/person/course-api/get-course-subject-info"
)
PLAYER_ORIGIN = "https://onlineroomse.pku.edu.cn"
BB_ORIGIN = "https://course.pku.edu.cn"

_PLAY_LINK_RE = re.compile(r'href="([^"]*playVideo\.action\?[^"]*token=[^"]+)"', re.I)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_CAS_IFRAME_RE = re.compile(r'<iframe[^>]+src="([^"]*yjapise[^"]*)"', re.I)
_M3U8_RE = re.compile(r"https?://[^\s\"'\\<>]+?\.m3u8[^\s\"'\\<>]*")

_HEADERS = ("名称", "时间", "教师", "操作")
# Each data cell repeats its own column label on narrow layouts, e.g.
# "时间: 2026-09-09 08:00:00".
_LABEL_RE = re.compile(rf"^({'|'.join(_HEADERS)})\s*[:：]\s*")


def list_recordings(client: httpx.Client, course_id: str) -> list[Recording]:
    resp = client.get(
        VIDEO_LIST,
        params={
            "course_id": course_id,
            "mode": "view",
            "sortDir": "ASCENDING",
            "editPaging": "true",
        },
    )
    resp.raise_for_status()
    return _parse_video_list(resp.text, course_id)


def _parse_video_list(html: str, course_id: str) -> list[Recording]:
    header: list[str] | None = None
    recordings: list[Recording] = []
    seen: set[str] = set()

    for row_html in _ROW_RE.findall(html):
        cells = [_text(cell) for cell in _CELL_RE.findall(row_html)]
        if not cells:
            continue

        link_match = _PLAY_LINK_RE.search(row_html)
        if link_match is None:
            if header is None and sum(cell in _HEADERS for cell in cells) >= 2:
                header = cells
            continue

        play_url = _unescape(link_match.group(1))
        if play_url in seen:
            continue
        seen.add(play_url)

        values = _map_cells(cells, header)
        recordings.append(
            Recording(
                course_id=course_id,
                title=values.get("名称") or cells[0] or "recording",
                recorded_at=values.get("时间", ""),
                teacher=values.get("教师", ""),
                play_url=play_url,
            )
        )
    return recordings


def _map_cells(cells: list[str], header: list[str] | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, cell in enumerate(cells):
        label = header[index] if header and index < len(header) else ""
        match = _LABEL_RE.match(cell)
        if match:
            label = match.group(1)
            cell = cell[match.end() :]
        if label in _HEADERS and cell:
            values.setdefault(label, cell)
    return values


def resolve_media(client: httpx.Client, recording: Recording) -> Recording:
    """Walk the player chain and fill in the recording's media fields.

    Returns the same object so callers can use it inline. Fields stay empty when
    the platform has not finished publishing the session.
    """
    if not recording.play_url:
        return recording

    play_page = client.get(f"{BB_ORIGIN}{STREAM_BASE}/{recording.play_url.lstrip('/')}")
    play_page.raise_for_status()

    cas_match = _CAS_IFRAME_RE.search(play_page.text)
    if not cas_match:
        # Older sessions embed the playlist directly in the Blackboard page.
        direct = _M3U8_RE.search(play_page.text)
        if direct:
            recording.m3u8_url = _unescape(direct.group(0))
        else:
            recording.unavailable_reason = _page_notice(play_page.text)
        return recording

    landed = client.get(_unescape(cas_match.group(1)), headers={"Referer": f"{BB_ORIGIN}/"})
    landed.raise_for_status()

    query = dict(
        part.split("=", 1)
        for part in landed.url.query.decode().split("&")
        if "=" in part
    )
    recording.platform_course_id = query.get("course_id", "")
    recording.subject_id = query.get("sub_id", "")
    if not (recording.platform_course_id and recording.subject_id):
        recording.unavailable_reason = "player did not expose course_id/sub_id"
        return recording

    info = client.get(
        SUBJECT_INFO,
        params={
            "course_id": recording.platform_course_id,
            "sub_id": recording.subject_id,
        },
        headers={"Referer": str(landed.url)},
    )
    info.raise_for_status()
    payload = info.json()
    if payload.get("code") != 0:
        recording.unavailable_reason = str(
            payload.get("message") or payload.get("msg") or f"api code {payload.get('code')}"
        )
        return recording

    _apply_subject_info(recording, payload.get("data") or {})
    if not recording.media_url:
        recording.unavailable_reason = "no playback URL in session metadata"
    return recording


def _apply_subject_info(recording: Recording, data: dict) -> None:
    recording.room = data.get("room_name") or recording.room
    try:
        recording.duration_seconds = int(data.get("duration") or 0)
    except (TypeError, ValueError):
        recording.duration_seconds = 0

    content = data.get("content") or {}
    recording.mp4_url = _pick_mp4(content)
    if not recording.mp4_url:
        recording.m3u8_url = _pick_m3u8(content)


def _pick_mp4(content: dict) -> str:
    """Prefer the campus-hosted copy; vendor URLs carry expiring tokens."""
    playback = content.get("playback") or {}
    candidates = [playback.get("url"), (content.get("save_playback") or {}).get("contents")]

    for entry in content.get("file_list") or []:
        name = entry.get("file_name") or ""
        if name.lower().endswith(".mp4"):
            candidates.append(name)

    candidates.append((content.get("firm_source") or {}).get("contents"))

    for candidate in candidates:
        if candidate and str(candidate).startswith("http"):
            return _unescape(str(candidate))
    return ""


def _pick_m3u8(content: dict) -> str:
    resource = content.get("resource")
    if isinstance(resource, str) and resource.strip():
        try:
            resource = json.loads(resource)
        except json.JSONDecodeError:
            resource = None

    if isinstance(resource, dict):
        multi = resource.get("multi_path") or {}
        for quality in ("fhd", "hd", "sd"):
            url = multi.get(quality)
            if url and str(url).startswith("http"):
                return _unescape(str(url))

        paths = resource.get("path")
        if isinstance(paths, list):
            for entry in paths:
                url = entry.get("url") if isinstance(entry, dict) else entry
                if url and str(url).startswith("http"):
                    return _unescape(str(url))
        elif isinstance(paths, str) and paths.startswith("http"):
            return _unescape(paths)

    blob = json.dumps(content, ensure_ascii=False)
    match = _M3U8_RE.search(blob)
    return _unescape(match.group(0)) if match else ""


def _page_notice(html: str) -> str:
    """Surface Blackboard's own explanation, e.g. a missing-permission notice."""
    notice = _text(html)
    for boilerplate in ("Insert title here",):
        notice = notice.replace(boilerplate, "")
    notice = notice.strip(" .")
    return notice[:200] or "no player on page"


def _text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(text.split()).strip()


def _unescape(url: str) -> str:
    return url.replace("&amp;", "&").strip()
