"""Announcements, course materials, assignments and deadlines.

Blackboard's public REST API is only half usable for a student account here:

  * `/courses/{id}/announcements` works and is the cleanest source.
  * `/courses/{id}/contents` lists the top-level areas correctly, but
    `/contents/{id}/children` returns an empty list for every folder even when
    `hasChildren` is true. PKU does not grant students the entitlement that
    endpoint needs, so the tree below the top level is read from the same page a
    browser would load, `content/listContent.jsp`.
  * `/calendars/items` reports assignment due dates across all courses at once,
    which the content tree does not carry.
"""

from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup, Tag

from .models import Announcement, Assignment, AttachmentRef, ContentItem

BB_API = "/learn/api/public/v1"
LIST_CONTENT = "/webapps/blackboard/content/listContent.jsp"
ANNOUNCEMENT_PAGE = "/webapps/blackboard/execute/announcement"

# Blackboard rejects anything larger with "Paging limit may not exceed 200".
PAGE_LIMIT = "200"

FOLDER_KIND = "内容文件夹"
ASSIGNMENT_KINDS = frozenset({"作业", "考试", "测试"})

# "已附加文件: TLCL-19.01.pdf ( 2.022 MB )" precedes the human description.
_ATTACHED_RE = re.compile(r"已附加文件\s*[:：]\s*\S.*?\(\s*[\d.]+\s*[KMGT]?B\s*\)\s*")
_SIZE_RE = re.compile(r"\(\s*([\d.]+)\s*([KMGT]?B)\s*\)")
_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
_XID_RE = re.compile(r"xid-(\d+_\d+)")


class ToolUnavailable(Exception):
    """The course has the tool switched off, or this account may not read it.

    Distinct from a real failure: several courses never enable announcements, so
    a sync should note it and move on rather than report a broken run.
    """


def _paginate(client: httpx.Client, path: str, params: dict | None = None) -> list[dict]:
    """Follow Blackboard's cursor pagination, which caps each page at 200."""
    query = dict(params or {})
    query.setdefault("limit", PAGE_LIMIT)
    results: list[dict] = []
    url: str | None = path
    first = True

    while url:
        resp = client.get(url, params=query if first else None)
        resp.raise_for_status()
        body = resp.json()
        results.extend(body.get("results", []))
        url = (body.get("paging") or {}).get("nextPage")
        first = False
    return results


def fetch_announcements(client: httpx.Client, course_id: str) -> list[Announcement]:
    try:
        raw_entries = _paginate(client, f"{BB_API}/courses/{course_id}/announcements")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (400, 403):
            raise ToolUnavailable(_api_message(exc.response)) from exc
        raise

    # The signed download tokens inside the REST payload are not honoured, so
    # fresh ones are taken from the page a browser would render.
    fresh = _announcement_asset_urls(client, course_id)

    announcements: list[Announcement] = []
    for raw in raw_entries:
        html = raw.get("body") or ""
        announcements.append(
            Announcement(
                course_id=course_id,
                announcement_id=raw.get("id", ""),
                title=raw.get("title") or "(无标题)",
                posted_at=raw.get("created") or raw.get("modified") or "",
                author=raw.get("creator") or "",
                body_html=html,
                body_text=_html_to_text(html),
                attachments=_embedded_attachments(html, fresh),
            )
        )
    announcements.sort(key=lambda a: a.posted_at, reverse=True)
    return announcements


def _announcement_asset_urls(client: httpx.Client, course_id: str) -> dict[str, str]:
    """Map each embedded file's xid to a download URL that actually works."""
    try:
        resp = client.get(
            ANNOUNCEMENT_PAGE,
            params={
                "method": "search",
                "context": "course_entry",
                "course_id": course_id,
                "handle": "announcements_entry",
                "mode": "view",
            },
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return {}

    urls: dict[str, str] = {}
    for raw_url in re.findall(r'(?:src|href)="([^"]*bbcswebdav[^"]*)"', resp.text):
        url = raw_url.replace("&amp;", "&")
        match = _XID_RE.search(url)
        if match:
            urls.setdefault(match.group(1), url)
    return urls


def _api_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message") or resp.status_code)
    except ValueError:
        return str(resp.status_code)


def walk_materials(
    client: httpx.Client, course_id: str, max_depth: int = 6
) -> tuple[list[ContentItem], list[Assignment]]:
    """Depth-first walk of a course's content areas.

    Returns the items found and, separately, the assignments encountered, since
    those are worth surfacing on their own even though they live in the tree.
    """
    items: list[ContentItem] = []
    assignments: list[Assignment] = []
    visited: set[str] = set()

    for area in _top_level_areas(client, course_id):
        _walk_folder(
            client,
            course_id,
            content_id=area["id"],
            parent_path=area["title"],
            depth=0,
            max_depth=max_depth,
            visited=visited,
            items=items,
            assignments=assignments,
        )
    return items, assignments


def _top_level_areas(client: httpx.Client, course_id: str) -> list[dict]:
    return [
        {"id": entry["id"], "title": entry["title"]}
        for entry in _paginate(client, f"{BB_API}/courses/{course_id}/contents")
        if entry.get("id") and entry.get("title")
    ]


def _walk_folder(
    client: httpx.Client,
    course_id: str,
    content_id: str,
    parent_path: str,
    depth: int,
    max_depth: int,
    visited: set[str],
    items: list[ContentItem],
    assignments: list[Assignment],
) -> None:
    if depth > max_depth or content_id in visited:
        return
    visited.add(content_id)

    try:
        resp = client.get(
            LIST_CONTENT, params={"course_id": course_id, "content_id": content_id}
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return

    for entry in _parse_content_list(resp.text, course_id, parent_path):
        item, child_id = entry
        items.append(item)

        if item.kind in ASSIGNMENT_KINDS:
            assignments.append(
                Assignment(
                    course_id=course_id,
                    title=item.title,
                    content_id=item.content_id,
                    instructions=item.body_text,
                    attachments=item.attachments,
                    source="content-tree",
                )
            )

        if child_id:
            _walk_folder(
                client,
                course_id,
                content_id=child_id,
                parent_path=item.path,
                depth=depth + 1,
                max_depth=max_depth,
                visited=visited,
                items=items,
                assignments=assignments,
            )


def _parse_content_list(
    html: str, course_id: str, parent_path: str
) -> list[tuple[ContentItem, str]]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("ul", id="content_listContainer")
    if not isinstance(container, Tag):
        return []

    results: list[tuple[ContentItem, str]] = []
    for li in container.find_all("li", recursive=False):
        if not isinstance(li, Tag) or "liItem" not in (li.get("class") or []):
            continue

        heading = li.find(["h3", "h4"])
        title = _collapse(heading.get_text(" ", strip=True)) if heading else ""
        if not title:
            continue

        icon = li.find("img", alt=True)
        kind = _collapse(icon["alt"]) if icon else ""

        child_id = ""
        attachments: list[AttachmentRef] = []
        for anchor in li.find_all("a", href=True):
            href = anchor["href"]
            if "listContent.jsp" in href:
                match = re.search(r"content_id=(_\d+_\d+)", href)
                if match:
                    child_id = match.group(1)
            elif "/bbcswebdav/" in href:
                attachments.append(
                    AttachmentRef(
                        filename=_collapse(anchor.get_text(" ", strip=True)) or "attachment",
                        url=href,
                    )
                )

        details = li.find("div", class_=lambda value: bool(value) and "details" in value)
        details_text = _collapse(details.get_text(" ", strip=True)) if details else ""
        _apply_sizes(attachments, details_text)

        results.append(
            (
                ContentItem(
                    course_id=course_id,
                    content_id=child_id or _own_id(li),
                    title=title,
                    kind=kind,
                    parent_path=parent_path,
                    body_text=_ATTACHED_RE.sub("", details_text).strip(),
                    attachments=attachments,
                ),
                child_id,
            )
        )
    return results


def _own_id(li: Tag) -> str:
    """Blackboard puts the item's own id on the <li>, as `contentListItem:_123_1`."""
    raw = li.get("id") or ""
    match = re.search(r"(_\d+_\d+)", raw)
    return match.group(1) if match else ""


def _apply_sizes(attachments: list[AttachmentRef], details_text: str) -> None:
    """Attach the sizes Blackboard prints next to each filename."""
    for attachment in attachments:
        index = details_text.find(attachment.filename)
        if index < 0:
            continue
        match = _SIZE_RE.search(details_text, index)
        if match:
            attachment.size = int(float(match.group(1)) * _UNITS.get(match.group(2), 1))


def fetch_deadlines(client: httpx.Client) -> dict[str, list[Assignment]]:
    """Assignment due dates for every course, keyed by Blackboard course id."""
    by_course: dict[str, list[Assignment]] = {}
    for raw in _paginate(client, f"{BB_API}/calendars/items"):
        course_id = raw.get("calendarId") or ""
        if not course_id.startswith("_"):
            continue  # personal and institution-wide entries
        by_course.setdefault(course_id, []).append(
            Assignment(
                course_id=course_id,
                title=raw.get("title") or "(无标题)",
                due_at=raw.get("end") or raw.get("start") or "",
                source="calendar",
            )
        )
    for entries in by_course.values():
        entries.sort(key=lambda a: a.due_at)
    return by_course


def merge_deadlines(assignments: list[Assignment], deadlines: list[Assignment]) -> list[Assignment]:
    """Fold calendar due dates into the assignments found in the content tree.

    The two sources name the same assignment identically but neither is a
    superset, so unmatched calendar entries are kept as their own records.
    """
    by_title = {a.title: a for a in assignments}
    merged = list(assignments)
    for deadline in deadlines:
        existing = by_title.get(deadline.title)
        if existing is not None:
            if not existing.due_at:
                existing.due_at = deadline.due_at
        else:
            merged.append(deadline)
    merged.sort(key=lambda a: (a.due_at or "9999", a.title))
    return merged


def _embedded_attachments(html: str, fresh_urls: dict[str, str]) -> list[AttachmentRef]:
    """Images and links instructors paste into announcement bodies."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, AttachmentRef] = {}
    for tag, attr in (("img", "src"), ("a", "href")):
        for node in soup.find_all(tag):
            url = node.get(attr) or ""
            if "/bbcswebdav/" not in url:
                continue
            match = _XID_RE.search(url)
            xid = match.group(1) if match else ""
            url = fresh_urls.get(xid, url)
            name = _collapse(node.get_text(" ", strip=True)) or (
                f"embedded-{xid}" if xid else "embedded"
            )
            found.setdefault(xid or url, AttachmentRef(filename=name, url=url))
    return list(found.values())


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4"]):
        block.append("\n")
    text = soup.get_text("", strip=False)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _collapse(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split()).strip()
