"""Read-only Blackboard gradebook queries over the Learn REST API.

Walks user → enrollments → per-course gradebook columns → per-column scores,
reusing the cookie session from :mod:`pku_sync.auth`. Only reads; never writes
grades (unlike the legacy ``webapps/gradebook/controller`` reconcile endpoints).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

USERS_ME = "https://course.pku.edu.cn/learn/api/public/v1/users/me"
USER_COURSES = "https://course.pku.edu.cn/learn/api/public/v1/users/{user_id}/courses"
COURSE_DETAIL = "https://course.pku.edu.cn/learn/api/public/v1/courses/{course_id}"
GRADEBOOK_COLUMNS = (
    "https://course.pku.edu.cn/learn/api/public/v2/courses/{course_id}/gradebook/columns"
)
GRADEBOOK_USERS = (
    "https://course.pku.edu.cn/learn/api/public/v2/courses/{course_id}/gradebook/columns/{column_id}/users"
)


def _grading_type(column: dict) -> str:
    """The column kind; Learn reports it as ``grading.type``."""
    grading = column.get("grading") or {}
    return str(grading.get("type") or grading.get("gradingType") or "")


def _skip_column(grading: str, name: str) -> bool:
    """Drop the Calculated 总分 aggregates that just duplicate per-item rows."""
    return grading == "Calculated" and "总计" in name and "平时" not in name


class GradesError(RuntimeError):
    pass


@dataclass(frozen=True)
class GradeRecord:
    course_name: str
    column_name: str
    score: float | None
    possible: float | None


def _results(client: httpx.Client, url: str) -> list[dict]:
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json().get("results") or []
    except (httpx.HTTPError, ValueError) as exc:
        raise GradesError(f"成绩接口失败 {url}：{exc}") from exc


def current_user_id(client: httpx.Client) -> str:
    """The signed-in user's Blackboard id (e.g. ``_170580_1``).

    ``/users/me`` answers with the user object itself, not the paged
    ``{"results": [...]}`` envelope the other endpoints use.
    """
    try:
        resp = client.get(USERS_ME)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GradesError(f"成绩接口失败 {USERS_ME}：{exc}") from exc
    user_id = (body or {}).get("id") if isinstance(body, dict) else None
    if not user_id:
        raise GradesError("拿不到当前用户信息（/users/me 未返回 id）")
    return str(user_id)


def course_title(client: httpx.Client, course_id: str) -> str:
    """A course's display name, falling back to its id.

    Enrollment records carry only ids, so the name needs its own lookup. A
    missing name must not sink the whole report, hence the quiet fallback.
    """
    try:
        resp = client.get(COURSE_DETAIL.format(course_id=course_id))
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError):
        return course_id
    name = body.get("name") if isinstance(body, dict) else None
    return str(name) if name else course_id


def student_courses(client: httpx.Client) -> list[tuple[str, str]]:
    """(course_id, course_name) for every Student-role enrollment."""
    user_id = current_user_id(client)
    enrollments = _results(client, USER_COURSES.format(user_id=user_id))
    courses: list[tuple[str, str]] = []
    for item in enrollments:
        if item.get("courseRoleId") != "Student":
            continue
        course_id = item.get("courseId")
        if not course_id:
            continue
        courses.append((course_id, course_title(client, course_id)))
    return courses


def course_grades(client: httpx.Client, course_id: str, course_name: str) -> list[GradeRecord]:
    """All graded columns for one course, mapped to GradeRecords."""
    records: list[GradeRecord] = []
    for column in _results(client, GRADEBOOK_COLUMNS.format(course_id=course_id)):
        grading = _grading_type(column)
        name = column.get("name") or ""
        if _skip_column(grading, name):
            continue
        column_id = column.get("id")
        if not column_id:
            continue
        possible = (column.get("score") or {}).get("possible")
        try:
            users = _results(
                client, GRADEBOOK_USERS.format(course_id=course_id, column_id=column_id)
            )
        except GradesError:
            users = []
        score = None
        if users:
            display = users[0].get("displayGrade") or {}
            maybe = display.get("score")
            if maybe is not None:
                score = float(maybe)
        records.append(
            GradeRecord(
                course_name=course_name,
                column_name=name,
                score=round(score, 2) if score is not None else None,
                possible=round(float(possible), 2) if possible is not None else None,
            )
        )
    return records


def fetch_all_grades(client: httpx.Client) -> list[GradeRecord]:
    """Every course's gradebook rows; a failing course is skipped, not fatal."""
    records: list[GradeRecord] = []
    for course_id, course_name in student_courses(client):
        try:
            records.extend(course_grades(client, course_id, course_name))
        except GradesError:
            continue
    return records
