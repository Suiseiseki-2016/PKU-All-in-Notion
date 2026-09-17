"""Unit tests for the Blackboard text-submission client (offline, fixtures only).

The fixtures mirror the markup the 2026-09-12 live session recorded in the
Notion page "Blackboard 作业提交自动化经验": single-quoted hidden inputs, the
review-history state after submission, and the JSON receipt on success.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pku_sync import submit as pku_submit
from pku_sync.submit import (
    AlreadySubmitted,
    SubmitError,
    deadline_warning,
    fetch_form,
    mime_type_for,
    parse_form,
    resolve_course_id,
    submit_file,
    submit_text,
)

# The real page renders hidden inputs with single quotes — the one-off
# script's regex had to be taught this; BeautifulSoup handles it natively.
FORM_HTML = """
<html><body>
<form id='assignment_form' method='post'>
  <input type='hidden' name='blackboard.platform.security.NonceUtil.nonce.ajax' value='nonce-abc-123'>
  <input type='hidden' name='course_id' value='_101578_1'>
  <input type='hidden' name='content_id' value='_1708913_1'>
  <input type='hidden' name='studentSubmission.text_f' value='encrypted-token'>
  <input type='hidden' name='studentSubmission.text_w' value='/webapps/assignment/submit'>
  <textarea name='studentSubmission.text' rows='8'></textarea>
</form>
</body></html>
"""

REVIEW_HISTORY_HTML = """
<html><body><h3>作业1 强化/惩罚 四象限</h3>
<div>复查提交历史记录</div>
</body></html>
"""

EMPTY_PAGE_HTML = "<html><body><h1>作业</h1><p>暂无内容</p></body></html>"


class FakeClient:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.calls: list[dict] = []

    def get(self, path, params=None):
        self.calls.append({"method": "get", "path": path, "params": params})
        return self

    def post(self, path, data=None, files=None, headers=None):
        self.calls.append(
            {"method": "post", "path": path, "data": data, "files": files, "headers": headers}
        )
        return self

    def json(self):
        return json.loads(self.text)


def test_parse_form_reads_single_quoted_hidden_inputs():
    form = parse_form(FORM_HTML, course_id="_101578_1", content_id="_1708913_1")
    assert form is not None
    assert form.nonce == "nonce-abc-123"
    assert form.hidden["studentSubmission.text_f"] == "encrypted-token"
    assert form.hidden["studentSubmission.text_w"] == "/webapps/assignment/submit"
    assert form.hidden["course_id"] == "_101578_1"


def test_parse_form_missing_nonce_is_none():
    html = "<form><input type='hidden' name='x' value='y'></form>"
    assert parse_form(html, "_1_1", "_2_1") is None
    assert parse_form(EMPTY_PAGE_HTML, "_1_1", "_2_1") is None


def test_fetch_form_returns_form_with_course_context():
    client = FakeClient(text=FORM_HTML)
    form = fetch_form(client, "_101578_1", "_1708913_1")
    assert form.course_id == "_101578_1"
    assert form.content_id == "_1708913_1"
    (call,) = client.calls
    assert call["path"] == "/webapps/assignment/uploadAssignment"
    assert call["params"] == {"content_id": "_1708913_1", "course_id": "_101578_1"}


def test_fetch_form_detects_already_submitted_state():
    client = FakeClient(text=REVIEW_HISTORY_HTML)
    with pytest.raises(AlreadySubmitted, match="已提交过"):
        fetch_form(client, "_101578_1", "_1708913_1")
    # The GET probe is expected; the guard must fire before any POST.
    assert [c for c in client.calls if c["method"] == "post"] == []


def test_fetch_form_surfaces_http_and_missing_form_errors():
    with pytest.raises(SubmitError, match="HTTP 500"):
        fetch_form(FakeClient(status_code=500, text="boom"), "_1_1", "_2_1")
    with pytest.raises(SubmitError, match="没有可解析的提交表单"):
        fetch_form(FakeClient(text=EMPTY_PAGE_HTML), "_1_1", "_2_1")


def test_submit_text_posts_multipart_with_ajax_header_and_all_hidden_fields():
    receipt = json.dumps({"destinationUrl": "/webapps/assignment/uploadAssignment?mode=DEFAULT"})
    client = FakeClient(text=receipt)
    form = parse_form(FORM_HTML, "_101578_1", "_1708913_1")

    body = submit_text(client, form, "正强化：发奖金增加上班行为。")

    assert body["destinationUrl"].endswith("mode=DEFAULT")
    (call,) = client.calls
    assert call["path"].startswith("/webapps/assignment/uploadAssignment?action=submit")
    # Every hidden input goes back, plus the answer field.
    assert call["data"]["blackboard.platform.security.NonceUtil.nonce.ajax"] == "nonce-abc-123"
    assert call["data"]["studentSubmission.text_f"] == "encrypted-token"
    assert call["data"]["studentSubmission.text_w"] == "/webapps/assignment/submit"
    assert call["data"]["studentSubmission.text"] == "正强化：发奖金增加上班行为。"
    # The two hard requirements from the live session: multipart + AJAX header.
    assert call["files"], "empty files entry must force multipart/form-data"
    assert call["headers"]["X-Requested-With"] == "XMLHttpRequest"


def test_submit_text_rejects_non_json_and_missing_receipt():
    with pytest.raises(SubmitError, match="响应不是 JSON"):
        submit_text(FakeClient(text="<html>err</html>"), _form(), "答案")
    with pytest.raises(SubmitError, match="缺少 destinationUrl"):
        submit_text(FakeClient(text='{"other": 1}'), _form(), "答案")
    with pytest.raises(SubmitError, match="HTTP 500"):
        submit_text(FakeClient(status_code=500, text="访问已拒绝"), _form(), "答案")


def _form():
    return parse_form(FORM_HTML, "_101578_1", "_1708913_1")


def test_fetch_form_passes_new_attempt_mode_for_file_submit():
    client = FakeClient(text=FORM_HTML)
    form = fetch_form(client, "_101578_1", "_1708913_1", action="newAttempt")
    assert form is not None
    (call,) = client.calls
    assert call["params"]["action"] == "newAttempt"
    assert call["params"]["content_id"] == "_1708913_1"


def test_mime_type_for_maps_common_extensions(tmp_path):
    assert mime_type_for(tmp_path / "answer.pdf") == "application/pdf"
    assert mime_type_for(tmp_path / "a.docx") == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert mime_type_for(tmp_path / "notes.md") == "text/markdown"
    assert mime_type_for(tmp_path / "img.png") == "image/png"
    assert mime_type_for(tmp_path / "unknown.zz9") == "application/octet-stream"


def test_submit_file_posts_multipart_with_file_part_and_filepicker_fields(tmp_path):
    answer_path = tmp_path / "answer.docx"
    answer_path.write_bytes(b"PK\x03\x04fake-docx-bytes")
    receipt = json.dumps({"destinationUrl": "/webapps/assignment/done"})
    client = FakeClient(text=receipt)
    form = parse_form(FORM_HTML, "_101578_1", "_1708913_1")

    body = submit_file(client, form, answer_path)

    assert body["destinationUrl"].endswith("/done")
    (call,) = client.calls
    assert call["path"].startswith("/webapps/assignment/uploadAssignment?action=submit")
    # Hidden inputs round-trip, plus the FilePicker contract fields.
    assert call["data"]["blackboard.platform.security.NonceUtil.nonce.ajax"] == "nonce-abc-123"
    assert call["data"]["course_id"] == "_101578_1"
    assert call["data"]["dispatch"] == "submit"
    assert call["data"]["newFile_linkTitle"] == "answer.docx"
    assert call["data"]["newFile_attachmentType"] == "L"
    assert call["data"]["newFilefilePickerLastInput"] == "dummyValue"
    # The file must be carried in the newFile_LocalFile0 part with its bytes.
    assert pku_submit.FILE_FIELD in call["files"]
    name, payload, content_type = call["files"][pku_submit.FILE_FIELD]
    assert name == "answer.docx"
    assert payload == b"PK\x03\x04fake-docx-bytes"
    assert content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert call["headers"]["X-Requested-With"] == "XMLHttpRequest"


def test_submit_file_accepts_empty_json_receipt(tmp_path):
    answer_path = tmp_path / "answer.md"
    answer_path.write_text("# answer", encoding="utf-8")
    client = FakeClient(text="")
    form = parse_form(FORM_HTML, "_101578_1", "_1708913_1")
    body = submit_file(client, form, answer_path)
    assert body["submitted"] is True
    assert body["file"] == "answer.md"


def test_submit_file_rejects_unreadable_and_http_failures(tmp_path):
    missing = tmp_path / "missing.pdf"
    with pytest.raises(SubmitError, match="读不了文件"):
        submit_file(FakeClient(text="{}"), _form(), missing)
    ok = tmp_path / "b.txt"
    ok.write_text("x", encoding="utf-8")
    with pytest.raises(SubmitError, match="HTTP 500"):
        submit_file(FakeClient(status_code=500, text="拒绝"), _form(), ok)


def test_resolve_course_id_accepts_blackboard_ids_and_name_fragments(tmp_path):
    course_dir = tmp_path / "认知心理学_26-27学年第1学期_101578"
    course_dir.mkdir()
    (course_dir / "course.json").write_text(
        json.dumps({"name": "认知心理学", "course_id": "_101578_1"}), encoding="utf-8"
    )
    assert resolve_course_id(tmp_path, "_101578_1") == "_101578_1"
    assert resolve_course_id(tmp_path, "认知") == "_101578_1"
    assert resolve_course_id(tmp_path, "认知心理学_26-27") == "_101578_1"  # folder fragment

    with pytest.raises(SubmitError, match="没有找到"):
        resolve_course_id(tmp_path, "不存在课")


def test_resolve_course_id_rejects_ambiguous_names(tmp_path):
    for folder, cid in (("心理测量_26-27学年第1学期", "_11_1"), ("组织管理心理学_26-27学年第1学期", "_22_2")):
        course_dir = tmp_path / folder
        course_dir.mkdir()
        (course_dir / "course.json").write_text(
            json.dumps({"name": folder.split("_")[0], "course_id": cid}), encoding="utf-8"
        )
    with pytest.raises(SubmitError, match="多门课程"):
        resolve_course_id(tmp_path, "心理")


def test_deadline_warning_flags_only_past_deadlines():
    now = datetime(2026, 9, 16, 12, 0)
    assert "逾期" in deadline_warning("2026-09-15 23:59", now=now)
    assert deadline_warning("2026-09-20 12:00", now=now) == ""
    assert deadline_warning("未公布", now=now) == ""


def test_snippet_collapses_whitespace():
    assert pku_submit._snippet("a\n  b\t\tc") == "a b c"
