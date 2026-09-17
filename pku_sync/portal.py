"""PKU portal (portal.pku.edu.cn) read-only queries: personal course table.

Mirrors the pku3b portal flow (MIT-licensed): IAAA OAuth under the
``portalPublicQuery`` appid, then two JSON endpoints for the current term's
course table. Nothing here writes anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .auth import iaaa_authenticate, new_client

PORTAL_APP_ID = "portalPublicQuery"
PORTAL_REDIR = "https://portal.pku.edu.cn/publicQuery/ssoLogin.do"
PORTAL_HOME = "https://portal.pku.edu.cn/publicQuery/"
XNDXQ_LIST = (
    "https://portal.pku.edu.cn/publicQuery/ctrl/topic/myCourseTable/getXndXqList.do"
)
COURSE_INFO = "https://portal.pku.edu.cn/publicQuery/ctrl/topic/myCourseTable/getCourseInfo.do"

_TAG_RE = re.compile(r"<[^>]*>")

_DAYS = (
    ("mon", "周一"),
    ("tue", "周二"),
    ("wed", "周三"),
    ("thu", "周四"),
    ("fri", "周五"),
    ("sat", "周六"),
    ("sun", "周日"),
)


class PortalError(RuntimeError):
    pass


def portal_session() -> httpx.Client:
    """Log into the portal with the blackboard credentials and return a client."""
    client = new_client()
    try:
        token = iaaa_authenticate(client, PORTAL_APP_ID, PORTAL_REDIR)
        redirect = client.get(PORTAL_REDIR, params={"token": token})
        redirect.raise_for_status()
        verify = client.get(PORTAL_HOME)
        verify.raise_for_status()
    except httpx.HTTPError as exc:
        raise PortalError(f"门户登录失败：{exc}") from exc
    return client


@dataclass(frozen=True)
class Term:
    """One selectable term, e.g. ``26-27-1`` / ``26-27学年1学期``."""

    code: str
    label: str


def fetch_terms(client: httpx.Client) -> tuple[str, list[Term]]:
    """``(current term code, every selectable term)`` from ``getXndXqList.do``."""
    try:
        resp = client.get(XNDXQ_LIST)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise PortalError(f"学期列表获取失败：{exc}") from exc
    current = (data.get("nowXnxq") or {}).get("xndxq")
    if not current:
        raise PortalError("门户未返回当前学年学期（nowXnxq.xndxq）")
    terms = [
        Term(code, str(entry.get("xndxqValue") or code))
        for entry in data.get("xndxq") or []
        if isinstance(entry, dict) and (code := entry.get("xndxq"))
    ]
    return current, terms


def fetch_course_table(client: httpx.Client, term: str | None = None) -> tuple[dict, str]:
    """``(raw course-table JSON, term used)``; defaults to the portal's current term.

    The body is returned unvalidated so callers can print it verbatim: the
    portal answers ``{"success": true, "message": "获取个人课表信息失败"}``
    (no ``course`` key) for a term whose table is not published yet.
    """
    used = term or fetch_terms(client)[0]
    try:
        resp = client.get(COURSE_INFO, params={"xndxq": used})
        resp.raise_for_status()
        return resp.json(), used
    except httpx.HTTPError as exc:
        raise PortalError(f"课表获取失败：{exc}") from exc


def table_unavailable(body: dict) -> str:
    """The portal's complaint when a term carries no course table, else ``""``."""
    if "course" in (body or {}):
        return ""
    return str((body or {}).get("message") or "门户未返回课表数据")


@dataclass(frozen=True)
class CourseSlot:
    """One day's consecutive-slot lecture run, ready to print."""

    day: str
    start_slot: int
    end_slot: int
    info: str


def parse_course_table(body: dict) -> list[CourseSlot]:
    """Flatten the portal JSON into per-day slot runs (pku3b's display shape).

    ``body["course"]`` is an array by the number of slots in the day graph;
    slot number = array index + 1. Each day column holds a course object (or
    null) whose ``courseName`` may embed 上课信息/教师/考试信息 fragments.
    """
    courses = (body or {}).get("course") or []
    slots: list[CourseSlot] = []
    for day_key, day_name in _DAYS:
        day_runs: list[CourseSlot] = []
        for index, slot in enumerate(courses):
            cell = (slot or {}).get(day_key)
            if not cell:
                continue
            name = (cell.get("courseName") or "").strip()
            if not name:
                continue
            info = format_course_info(name)
            if day_runs and day_runs[-1].end_slot == index and day_runs[-1].info == info:
                day_runs[-1] = CourseSlot(day_name, day_runs[-1].start_slot, index + 1, info)
            else:
                day_runs.append(CourseSlot(day_name, index + 1, index + 1, info))
        slots.extend(day_runs)
    return slots


def _clean(text: str) -> str:
    """Drop HTML markup and collapse the portal's padded whitespace."""
    return " ".join(_TAG_RE.sub(" ", text).split()).strip()


def _cut(text: str, *stops: str) -> str:
    """``text`` up to whichever of ``stops`` appears first, cleaned."""
    end = len(text)
    for stop in stops:
        found = text.find(stop)
        if found != -1:
            end = min(end, found)
    return _clean(text[:end])


def course_name(info: str) -> str:
    """The bare course name from a packed ``courseName`` cell.

    The portal wraps names in markup (``<font color='red'><b>``), appends the
    enrolment kind (``(主)`` primary major, ``(辅双)`` minor/dual degree), then
    concatenates the detail blocks. Only ``(主)`` is dropped: the other markers
    distinguish otherwise identically named courses.
    """
    head = _clean(info.split("上课信息：", 1)[0])
    return head.split("(主)", 1)[0].strip()


def format_course_info(info: str) -> str:
    """Trim the packed name into ``课程名 ｜ 上课 ｜ 教师 ｜ 备注 ｜ 考试``."""
    parts = [course_name(info)]
    if "上课信息：" in info:
        rest = info.split("上课信息：", 1)[1]
        if where := _cut(rest, "教师：", "备注：", "考试信息："):
            parts.append(f"上课：{where}")
        if "教师：" in rest:
            if teacher := _cut(rest.split("教师：", 1)[1], "备注：", "考试信息："):
                parts.append(f"教师：{teacher}")
        if "备注：" in rest:
            if remark := _cut(rest.split("备注：", 1)[1], "考试信息："):
                parts.append(f"备注：{remark}")
    if "考试信息：" in info:
        if exam := _cut(info.split("考试信息：", 1)[1]):
            parts.append(f"考试：{exam}")
    return " ｜ ".join(part for part in parts if part)


def render_course_table(body: dict) -> list[str]:
    """Human-friendly ``[周一] 第2-4节: …`` lines for the CLI."""
    slots = parse_course_table(body)
    if not slots:
        return ["暂无课表数据"]
    lines: list[str] = ["个人课表"]
    for slot in slots:
        span = (
            f"第{slot.start_slot}节"
            if slot.start_slot == slot.end_slot
            else f"第{slot.start_slot}-{slot.end_slot}节"
        )
        lines.append(f"[{slot.day}] {span}：{slot.info}")
    return lines
