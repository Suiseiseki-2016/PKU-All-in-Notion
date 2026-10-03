"""Conservative, source-linked deadlines from official course announcements."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

_DATE = re.compile(
    r"(?:(?P<year>20\d{2})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
    r"(?:\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)
_DELIVERY = re.compile(r"提交|上交|交至|交到|发送|填报|递交|交付")
_BEIJING = timezone(timedelta(hours=8))


def announcement_source_url(course_id: str, announcement_id: str = "") -> str:
    """The browser list anchors each announcement by its stable Blackboard ID."""
    base = ("https://course.pku.edu.cn/webapps/blackboard/execute/announcement"
            "?method=search&context=course_entry&course_id=" + quote(course_id)
            + "&handle=announcements_entry&mode=view")
    if re.fullmatch(r"_\d+_\d+", announcement_id or ""):
        return base + "#" + announcement_id
    return base


def _posted_year(posted_at: str, month: int) -> int:
    try:
        posted = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if posted.tzinfo:
            posted = posted.astimezone(_BEIJING)
        return posted.year + int(month < posted.month - 6)
    except ValueError:
        return datetime.now(_BEIJING).year


def announcement_tasks(course_id: str, notices: list[dict]) -> list[dict]:
    """Extract only lines with both a date and an explicit delivery instruction.

    These remain announcement-sourced reminders, never a verified upload target.
    The exact teacher wording is retained for review.
    """
    tasks: list[dict] = []
    for notice in notices:
        if not isinstance(notice, dict):
            continue
        notice_id = str(notice.get("id") or "")
        title = str(notice.get("title") or "公告")
        for line in str(notice.get("body_text") or "").splitlines():
            sentence = " ".join(line.split())
            if not _DELIVERY.search(sentence):
                continue
            for match in _DATE.finditer(sentence):
                try:
                    month, day = int(match["month"]), int(match["day"])
                    year = int(match["year"] or _posted_year(str(notice.get("posted_at") or ""), month))
                    hour = int(match["hour"] or 23)
                    minute = int(match["minute"] or 59)
                    due = datetime(year, month, day, 0 if hour == 24 else hour, minute,
                                   tzinfo=_BEIJING)
                    if hour == 24:
                        due += timedelta(days=1)
                except ValueError:
                    continue
                label = match.group(0)
                identity = f"{course_id}|{notice_id}|{label}|{sentence}"
                tasks.append({
                    "id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
                    "title": f"{title} · {label}",
                    "notice_title": title,
                    "announcement_id": notice_id,
                    "due_at": due.isoformat(),
                    "due_at_label": label + "（老师公告原文）",
                    "instructions": sentence,
                    "source": "announcement",
                    "source_url": announcement_source_url(course_id, notice_id),
                })
    return sorted(tasks, key=lambda item: (item["due_at"], item["title"]))


def distinct_announcement_tasks(course_id: str, notices: list[dict],
                                assignments: list[dict]) -> list[dict]:
    """Avoid a second reminder when the same dated task has a formal card."""
    tasks = announcement_tasks(course_id, notices)
    formal = []
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        title = str(assignment.get("title") or "").strip()
        raw = str(assignment.get("due_at") or "")
        try:
            due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if due.tzinfo:
                due = due.astimezone(_BEIJING)
            formal.append((title, due.date()))
        except ValueError:
            continue
    return [task for task in tasks
            if not any(title and title in task["instructions"]
                       and datetime.fromisoformat(task["due_at"]).date() == day
                       for title, day in formal)]
