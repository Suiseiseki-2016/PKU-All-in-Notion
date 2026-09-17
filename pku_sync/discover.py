"""Discover every course the authenticated user can see.

Blackboard exposes course membership several ways and PKU does not enable all
of them, so the strategies below run in order and the results are merged. Each
course records which strategy found it, which makes a failing strategy visible
instead of silently reducing coverage.
"""

from __future__ import annotations

import re
from collections import Counter

import httpx

from .models import Course

BB_API = "/learn/api/public/v1"

# Blackboard course ids look like _104128_1; the module ids that also appear in
# portal markup (for example _4_1) are far shorter, so require 4+ digits.
_COURSE_ID_RE = re.compile(r"course_id=(_\d{4,}_\d+)")
_ANCHOR_RE = re.compile(
    r'<a[^>]+href="[^"]*course_id=(_\d{4,}_\d+)[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")

_PORTAL_PAGES = (
    "/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_2_1",
    "/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1",
    "/webapps/portal/execute/defaultTab",
)


_TERM_RE = re.compile(r"^(\d{5})-")


def discover_courses(client: httpx.Client) -> list[Course]:
    found: dict[str, Course] = {}
    for strategy in (_from_rest, _from_portal):
        try:
            for course in strategy(client):
                found.setdefault(course.course_id, course)
        except Exception:
            # A single unavailable strategy must not abort discovery; the
            # `source` field on the results shows which ones contributed.
            continue
    return assign_directories(sorted(found.values(), key=lambda c: (c.name, c.course_id)))


def assign_directories(courses: list[Course]) -> list[Course]:
    """Give each course a unique directory name.

    PKU runs some courses as several sections that share one name, so the name
    alone would make two courses write over each other. The suffix is derived
    from the whole discovered list rather than the selected subset, so a course
    keeps the same directory whether one course or all of them are synced.
    """
    counts = Counter(course.slug for course in courses)
    for course in courses:
        if counts[course.slug] > 1:
            match = re.search(r"\d+", course.course_id)
            suffix = match.group(0) if match else course.course_id.strip("_")
            course.dir_name = f"{course.slug}_{suffix}"
        else:
            course.dir_name = course.slug
    return courses


def term_of(course: Course) -> str:
    """Return the 5-digit term code encoded in a course's Blackboard code."""
    match = _TERM_RE.match(course.code or "")
    return match.group(1) if match else ""


def current_term(courses: list[Course]) -> str:
    terms = {term_of(c) for c in courses}
    terms.discard("")
    return max(terms) if terms else ""


def select_courses(courses: list[Course], settings) -> list[Course]:
    """Apply term, allowlist and denylist filters from settings."""
    picked = [c for c in courses if settings.course_selected(c.course_id)]

    if settings.allowlist or settings.include_past_terms:
        # An explicit allowlist is a deliberate choice, so term filtering would
        # only get in the way.
        return picked

    target = settings.course_term or current_term(picked)
    if not target:
        return picked
    return [c for c in picked if term_of(c) == target or not c.code]


def _from_rest(client: httpx.Client) -> list[Course]:
    resp = client.get(
        f"{BB_API}/users/me/courses",
        params={"expand": "course", "limit": "200"},
    )
    resp.raise_for_status()
    courses: list[Course] = []
    for membership in resp.json().get("results", []):
        course = membership.get("course") or {}
        course_id = course.get("id") or membership.get("courseId") or ""
        if not course_id:
            continue
        courses.append(
            Course(
                course_id=course_id,
                name=course.get("name") or course_id,
                code=course.get("courseId", ""),
                term=(course.get("term") or {}).get("name", ""),
                source="rest",
            )
        )
    return courses


def _from_portal(client: httpx.Client) -> list[Course]:
    courses: dict[str, Course] = {}
    for page in _PORTAL_PAGES:
        try:
            resp = client.get(page)
            resp.raise_for_status()
        except httpx.HTTPError:
            continue

        html = resp.text
        for course_id, label in _ANCHOR_RE.findall(html):
            name = _clean_label(label)
            if not name:
                continue
            courses.setdefault(
                course_id,
                Course(course_id=course_id, name=name, source="portal"),
            )

        # Ids that appear only outside anchors still get recorded so the caller
        # can resolve their names from the course page itself.
        for course_id in _COURSE_ID_RE.findall(html):
            courses.setdefault(
                course_id,
                Course(course_id=course_id, name=course_id, source="portal-id-only"),
            )
    return list(courses.values())


def _clean_label(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(text.split()).strip()


def resolve_course_name(client: httpx.Client, course_id: str) -> str:
    """Read a course's display name off its own landing page."""
    resp = client.get(
        "/webapps/blackboard/execute/announcement",
        params={"method": "search", "course_id": course_id},
    )
    resp.raise_for_status()
    match = re.search(r"<title>(.*?)</title>", resp.text, re.DOTALL)
    if match:
        title = _clean_label(match.group(1))
        # Blackboard renders "<course name> – Blackboard Learn"
        for separator in ("–", "-", "|"):
            if separator in title:
                candidate = title.split(separator)[0].strip()
                if candidate:
                    return candidate
        if title:
            return title
    return course_id


def discover_selected_courses(
    client: httpx.Client,
    settings,
    discovered: list[Course] | None = None,
    discover_fn=discover_courses,
    resolve_name_fn=resolve_course_name,
    select_fn=select_courses,
) -> list[Course]:
    """Discover courses, name the id-only placeholders, then apply settings filters.

    Used by the TUI's live snapshot so its course set matches what the CLI's
    course picker would select for the same term/allowlist. A course whose
    landing page cannot be read keeps its placeholder id rather than being
    dropped. Pass ``discovered`` to reuse an already-fetched course list
    instead of calling ``discover_fn`` a second time.
    """
    courses = discover_fn(client) if discovered is None else discovered
    for course in courses:
        if course.name == course.course_id:
            try:
                course.name = resolve_name_fn(client, course.course_id)
            except Exception:
                pass
    return select_fn(courses, settings)
