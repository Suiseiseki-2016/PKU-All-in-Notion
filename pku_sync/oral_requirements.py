"""Conservative, source-linked leads for coursework mentioned in a recording.

ASR has no reliable speaker labels.  A ``confirmed`` lead means the *wording*
is affirmative; it is still a transcript lead to check against the recording
and the teaching network, not a verified formal assignment.  This module
never manufactures a question, attachment, submission URL, or due date.
"""

from __future__ import annotations

import re
from typing import Any


_QUESTION = re.compile(r"[？?]|(?:是不是|什么时候|多少次|有几次|在哪(?:里|儿)|怎么交|能不能|可以吗)")
_TENTATIVE = re.compile(
    r"(?:大概|可能|估计|左右|应该|感觉|看情况|到时候|再看|协商|如果|计划|往年|以前|不确定|尚未)"
)
_NEGATIVE = re.compile(r"(?:不(?:用|需要|必|会)|无需|不用|还没|尚未|没有|未(?:布置|发布|确定))")
_TASK = re.compile(r"(?:作业|练习|实验|报告|展示|考试|测验|小测|考核)")
_ASSIGN = re.compile(r"(?:布置|发布|发到|发在|交|提交|上传|完成|做)")
_LOCATION = re.compile(r"(?:教学网|教育网|学网|课程作业|作业(?:区|文件夹|栏目)|在线|网上|实验平台)")
_DEADLINE = re.compile(r"(?:截止|之前|以前|前交|前提交|最晚|不得晚于)")
_DATE = re.compile(
    r"(?:\d{1,2}\s*月\s*\d{1,2}\s*[日号]?|\d{1,2}\s*[日号]|"
    r"(?:本|这|下|下下)?周[一二三四五六日天]?|(?:今|明|后)天|"
    r"\d{1,2}\s*[:：]\s*\d{2})"
)
_ATTENDANCE = re.compile(r"(?:考勤|签到|点名|到场|到课|来上课|现场展示|课堂展示|课堂汇报)")
_OBLIGATION = re.compile(r"(?:必须|需要|要|须|要求|请|记得|得|扣分|计入|算作)")
_QUANTITY = re.compile(r"(?:\d+|[一二三四五六七八九十两]+)\s*(?:次|份|个|篇|组)")


def _row(segment: dict[str, Any]) -> dict[str, Any] | None:
    quote = str(segment.get("text") or "").strip()
    if not quote:
        return None
    try:
        start = float(segment["start"])
        end = float(segment.get("end", start))
    except (KeyError, ValueError, TypeError):
        return None
    if start < 0 or end < start:
        return None
    return {"start": start, "end": end, "quote": quote}


def _lead(kind: str, status: str, evidence: list[dict[str, Any]],
          *, location: str | None = None, quantities: list[str] | None = None,
          deadline_spoken: str | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "status": status,
        "evidence": evidence,
        "start": evidence[0]["start"],
        "end": evidence[-1]["end"],
        "location_spoken": location,
        "quantities_spoken": quantities or [],
        "deadline_spoken": deadline_spoken,
        "formal_assignment_verified": False,
        "speaker_verified": False,
        "needs_recording_review": True,
    }


def _location(text: str) -> str | None:
    """Return only the place literally present in the utterance."""
    match = re.search(r"(?:教学网|教育网|学网|课程作业(?:的)?(?:那个)?(?:文件夹|区|栏目)?|实验平台)", text)
    return match.group() if match else None


def _has_uncertain_context(rows: list[dict[str, Any]], index: int) -> bool:
    text = rows[index]["quote"]
    if _TENTATIVE.search(text):
        return True
    # A short ASR segment may contain only the number after an uncertain
    # opening clause; keep that clause's qualification when nearby.
    for prior in rows[max(0, index - 2):index]:
        if rows[index]["start"] - prior["end"] > 5:
            continue
        if _TENTATIVE.search(prior["quote"]):
            return True
    return False


def _preceding_task_question(rows: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    """Find an adjacent question, ignoring one short ASR hesitation."""
    for offset in (1, 2):
        if index < offset:
            break
        candidate = rows[index - offset]
        if rows[index]["start"] - candidate["end"] > 12:
            break
        if _QUESTION.search(candidate["quote"]) and re.search(r"作业|任务|练习", candidate["quote"]):
            return candidate
        if len(candidate["quote"].strip("呃嗯啊 ，。")) > 0:
            break
    return None


def extract_oral_requirements(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract conservative coursework leads with exact ASR quotes and time.

    ``confirmed`` denotes affirmative speech, ``tentative`` a plan/estimate,
    and ``question`` is deliberately omitted.  The caller must still check
    the cited video and formal assignment cards before student-facing claims.
    """
    rows = [row for segment in segments if (row := _row(segment))]
    leads: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        text = row["quote"]
        if _QUESTION.search(text) or _NEGATIVE.search(text):
            continue
        uncertain = _has_uncertain_context(rows, index)
        status = "tentative" if uncertain else "confirmed"
        previous = rows[index - 1] if index else None
        context = (previous["quote"] + " " + text) if previous and row["start"] - previous["end"] <= 8 else text

        # A short answer such as “今天就布置” is meaningful only when the
        # immediately preceding question explicitly concerns an assignment.
        task_question = _preceding_task_question(rows, index)
        assignment_reply = bool(task_question and re.search(r"布置|发布|发", text))
        declarative_assignment = bool(
            _TASK.search(text) and re.search(r"布置|发布", text)
            and not re.search(r"(?:你|您).{0,8}(?:布置|发布)", text)
        )
        if declarative_assignment or assignment_reply:
            evidence = [task_question, row] if assignment_reply else [row]
            leads.append(_lead("assignment_announcement", status, evidence))

        # Report a literal location only alongside an explicit coursework
        # noun/action. A generic mention of the teaching network is not a
        # submission instruction.
        explicit_submission = bool(re.search(r"^\s*(?:对+|是的|没错|就是).{0,24}(?:作业|提交|教学网|教育网|学网|课程作业)", text)
                                   or re.search(r"(?:作业|报告|练习).{0,20}(?:交|提交|上传)", text))
        location_continuation = bool(
            previous and re.search(r"(?:作业|任务|练习)(?:呢|就是|在|的)", previous["quote"])
            and row["start"] - previous["end"] <= 8
            and re.search(r"(?:课程作业|作业(?:区|文件夹|栏目))", text)
        )
        if (_LOCATION.search(text) and (explicit_submission or location_continuation)
                and re.search(r"作业|练习|报告|实验", context)
                and not re.search(r"(?:考|复习)试.*(?:作业|提交)", text)):
            evidence = [row]
            if location_continuation and previous:
                evidence = [previous, row]
            # A later, separately qualified experiment plan does not turn a
            # preceding direct written-homework location into a plan.
            spoken_location = _location(text)
            before_location = text.split(spoken_location, 1)[0] if spoken_location else text
            location_status = "tentative" if _TENTATIVE.search(before_location) else "confirmed"
            leads.append(_lead("submission_location", location_status, evidence,
                               location=_location(text)))

        if re.search(r"实验作业|实验任务|实验练习", text) and re.search(r"实验平台|平台", text) and uncertain:
            leads.append(_lead("possible_experiment_platform", "tentative", [row],
                               location=_location(text[text.find("实验"):]) or "实验平台"))

        if _TASK.search(text) and _QUANTITY.search(text):
            quantities = [match.group() for match in _QUANTITY.finditer(text)]
            leads.append(_lead("workload_estimate" if uncertain else "workload_count",
                               status, [row], quantities=quantities))

        if _TASK.search(text) and _DEADLINE.search(text) and _DATE.search(text):
            # Dates without a due-time expression (e.g. "今天布置作业") are
            # not deadlines.  Preserve literal wording; never compute a date.
            due_marker = _DEADLINE.search(text)
            dates = list(_DATE.finditer(text))
            due = min(dates, key=lambda match: abs(match.start() - due_marker.start())).group()
            leads.append(_lead("spoken_deadline", status, [row],
                               deadline_spoken=due))

        if (_ATTENDANCE.search(text) and _OBLIGATION.search(text)
                and not re.search(r"(?:往年|以前|如果|可能|考虑)", text)):
            leads.append(_lead("attendance_obligation", status, [row]))

    # Near-identical ASR repeats create noise; retain the first quote and
    # later distinct wording or timings for audit rather than merging claims.
    seen: set[tuple[str, str, str]] = set()
    result = []
    for lead in leads:
        key = (lead["kind"], lead["evidence"][-1]["quote"], lead["status"])
        if key not in seen:
            seen.add(key)
            result.append(lead)
    return result
