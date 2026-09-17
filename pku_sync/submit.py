"""Text-assignment submission to course.pku.edu.cn (Blackboard Learn).

Rebuilt from the 2026-09-12 live reverse-engineering session, recorded in
the Notion page “Blackboard 作业提交自动化经验（2026-09-12）”. The one-off
script from that session (scripts/_submit4.py) was lost in a later cleanup;
this module is its productized form, with the same wire behavior:

- GET ``/webapps/assignment/uploadAssignment?content_id=…&course_id=…``
  renders the submit form. Hidden inputs carry the AJAX nonce
  (``blackboard.platform.security.NonceUtil.nonce.ajax``) plus the two
  ``studentSubmission`` session fields (``text_f`` / ``text_w``). The page
  doubles as the state probe: once an assignment is submitted the form is
  gone and the page shows the review-history state instead — that is the
  duplicate-submission guard.
- POST ``/webapps/assignment/uploadAssignment?action=submit`` must be
  ``multipart/form-data`` (an empty ``files`` entry forces multipart even
  with no attachment) and must carry ``X-Requested-With: XMLHttpRequest``.
  Missing either fails with 500 (“访问已拒绝” / “not
  MultipartHttpServletRequest”).
- Success is HTTP 200 with ``{"destinationUrl": …}`` (text) or an empty
  body (file).

File attachments (docx/pdf/zip/…) POST to the same endpoint with the file
in the ``newFile_LocalFile0`` multipart part plus Blackboard's FilePicker
fields; the wire format mirrors pku3b (MIT-licensed), which reverse-engaged
the same 2026-09 upload flow.
"""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from .tui_data import parse_due

NONCE_KEY = "blackboard.platform.security.NonceUtil.nonce.ajax"
TEXT_FIELD = "studentSubmission.text"
TEXT_F_KEY = "studentSubmission.text_f"
TEXT_W_KEY = "studentSubmission.text_w"
SUBMIT_PATH = "/webapps/assignment/uploadAssignment"

# File-attachment part name and the FilePicker fields Blackboard expects
# alongside it (mirrored from the pku3b wire format; pku3b is MIT-licensed
# and reverse-engineered the same 2026-09 upload flow).
FILE_FIELD = "newFile_LocalFile0"
FILE_EXTRA_FIELDS = {
    "studentSubmission.text": "",
    "student_commentstext": "",
    "dispatch": "submit",
    "newFile_artifactFileId": "undefined",
    "newFile_artifactType": "undefined",
    "newFile_artifactTypeResourceKey": "undefined",
    "newFile_attachmentType": "L",  # attachment, not link
    "newFile_fileId": "new",
    "newFilefilePickerLastInput": "dummyValue",
}
_FILE_MIME_OVERRIDES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".zip": "application/zip",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def mime_type_for(path: Path) -> str:
    """Content type for the attachment part; octet-stream as the last resort."""
    guess = mimetypes.guess_type(path.name)[0]
    return _FILE_MIME_OVERRIDES.get(path.suffix.lower(), guess) or "application/octet-stream"

# Once submitted, the form page is replaced by the review-history view.
_REVIEW_MARKERS = ("复查提交历史记录", "Review Submission History")

_BB_ID_RE = re.compile(r"^_\d+_\d+$")
_BLACKBOARD_COURSE_ID_RE = re.compile(r'"course_id"\s*:\s*"(_\d+_\d+)"')


class SubmitError(RuntimeError):
    """Submission cannot proceed; the message explains why."""


class AlreadySubmitted(SubmitError):
    """The form page shows the review-history state (already submitted)."""


@dataclass(frozen=True)
class SubmissionForm:
    """Every hidden input of the submit form, ready to be posted back."""

    course_id: str
    content_id: str
    hidden: dict[str, str]

    @property
    def nonce(self) -> str:
        return self.hidden.get(NONCE_KEY, "")

    def fields(self, text: str) -> dict[str, str]:
        data = dict(self.hidden)
        data[TEXT_FIELD] = text
        return data


def parse_form(html: str, course_id: str, content_id: str) -> SubmissionForm | None:
    """Collect the form's hidden inputs; None when the form is absent.

    BeautifulSoup is used instead of the regex the one-off script needed:
    it tolerates the page's single-quoted attribute style and any attribute
    order for free.
    """
    soup = BeautifulSoup(html, "html.parser")
    hidden = {
        node.get("name", ""): node.get("value", "")
        for node in soup.find_all("input")
        if (node.get("type", "") or "").lower() == "hidden" and node.get("name")
    }
    if NONCE_KEY not in hidden or TEXT_F_KEY not in hidden or TEXT_W_KEY not in hidden:
        return None
    return SubmissionForm(course_id=course_id, content_id=content_id, hidden=hidden)


def fetch_form(
    client, course_id: str, content_id: str, action: str | None = None
) -> SubmissionForm:
    """Fetch and validate the submit form, or explain why it is unavailable.

    ``action="newAttempt"`` is the mode the file-upload path uses (mirrors
    pku3b); the text path leaves it to Blackboard's default. Raises
    AlreadySubmitted when the review-history state is detected — the
    natural idempotency guard: an already-submitted assignment never gets a
    second POST from this module.
    """
    params = {"content_id": content_id, "course_id": course_id}
    if action:
        params["action"] = action
    resp = client.get(SUBMIT_PATH, params=params)
    if resp.status_code != 200:
        raise SubmitError(f"取表单失败（HTTP {resp.status_code}）：{_snippet(resp.text)}")
    html = resp.text
    if any(marker in html for marker in _REVIEW_MARKERS):
        raise AlreadySubmitted("该作业已提交过（页面为复查提交历史状态），不做重复提交。")
    form = parse_form(html, course_id=course_id, content_id=content_id)
    if form is None:
        raise SubmitError("页面没有可解析的提交表单（可能未开放、非文本作业入口或无权限）。")
    return form


def submit_text(client, form: SubmissionForm, text: str) -> dict:
    """POST the answer as a text submission and return Blackboard's receipt.

    The receipt (``{"destinationUrl": …}``) is what gets written back to
    the Notion task row as the submission receipt.
    """
    resp = client.post(
        f"{SUBMIT_PATH}?action=submit",
        data=form.fields(text),
        # data + files is what makes httpx send multipart/form-data; the
        # empty entry is never a real file part, it just forces the encoding
        # Blackboard demands ("not MultipartHttpServletRequest" otherwise).
        files={"_force_multipart": (None, "")},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    if resp.status_code != 200:
        raise SubmitError(f"提交被拒（HTTP {resp.status_code}）：{_snippet(resp.text)}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise SubmitError(f"响应不是 JSON：{_snippet(resp.text)}") from exc
    if not isinstance(body, dict) or "destinationUrl" not in body:
        raise SubmitError(f"响应缺少 destinationUrl：{_snippet(resp.text)}")
    return body


def submit_file(client, form: SubmissionForm, path: Path) -> dict:
    """POST a file attachment (docx/pdf/zip/…) as the assignment submission.

    Same endpoint and AJAX multipart contract as the text path; the file is
    carried in the ``newFile_LocalFile0`` part with the FilePicker fields
    Blackboard requires (see FILE_EXTRA_FIELDS). Blackboard may answer a
    file submit with an empty body, so a receipt is optional: the return
    value is the JSON response when present, else ``{"submitted": True}``.
    """
    filename = path.name
    content_type = mime_type_for(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SubmitError(f"读不了文件 {path}: {exc}") from exc

    data = dict(form.hidden)
    data.update(FILE_EXTRA_FIELDS)
    data["newFile_linkTitle"] = filename

    resp = client.post(
        f"{SUBMIT_PATH}?action=submit",
        data=data,
        files={FILE_FIELD: (filename, raw, content_type)},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    if resp.status_code != 200:
        raise SubmitError(f"提交被拒（HTTP {resp.status_code}）：{_snippet(resp.text)}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if body:
        return body
    return {"submitted": True, "file": filename, "content_type": content_type}


def resolve_course_id(data_dir: Path, course: str) -> str:
    """Accept a Blackboard course id as-is, or resolve a name/folder fragment.

    Name matching reads the synced ``<course>/course.json`` files, so
    ``--course 认知心理学`` works without remembering ``_101578_1``.
    """
    course = course.strip()
    if _BB_ID_RE.match(course):
        return course
    needle = course.lower()
    matches: list[tuple[str, str]] = []
    data_dir = data_dir.expanduser()
    if data_dir.is_dir():
        import json

        for meta in sorted(data_dir.glob("*/course.json")):
            try:
                item = json.loads(meta.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            name = str(item.get("name") or "")
            course_id = str(item.get("course_id") or "")
            if course_id and (needle in name.lower() or needle in meta.parent.name.lower()):
                matches.append((name or meta.parent.name, course_id))
    if len(matches) == 1:
        return matches[0][1]
    if not matches:
        raise SubmitError(
            f"没有找到名字包含 {course!r} 的课程；请用 `pku-sync submit list` 前先跑过 sync，"
            "或直接传 Blackboard course_id（形如 _101578_1）。"
        )
    listing = "；".join(f"{name} → {cid}" for name, cid in matches)
    raise SubmitError(f"{course!r} 匹配到多门课程：{listing}。请用 course_id 精确指定。")


def deadline_warning(due_at: str, now: datetime | None = None) -> str:
    """Loud warning when the deadline has passed; empty string otherwise."""
    due = parse_due(due_at)
    if due is None:
        return ""
    if due < (now or datetime.now()):
        return f"⚠️ 截止时间 {due_at} 已过，这属于逾期提交——提交前请确认确实还要交。"
    return ""


def _snippet(text: str, limit: int = 200) -> str:
    """Compact one-line excerpt of an error page for log/message inclusion."""
    return " ".join(text.split())[:limit]
